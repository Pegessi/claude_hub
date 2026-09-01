"""RED tests for the bounded agent-stream attachment preview cache.

The cache stores only browser-generated bounded previews (never the original
image bytes or data URLs). Three explicit limits are enforced with
oldest-created-first (FIFO) eviction:

* per-preview byte cap
* per-session total bytes / count
* global total bytes / count

The event payload stores only an opaque attachment id, mime type, byte size,
and optional dimensions — never a local path or raw bytes. The GET endpoint
validates the opaque id and session ownership; evicted/missing previews
render a stable placeholder rather than breaking the turn.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from claude_hub.services import workspace_manager

# ── minimal valid image bytes ───────────────────────────────────────────────
# These helpers produce bytes that pass the magic-byte and dimension-parsing
# checks. They are not real decodable images — only the header bytes that the
# store inspects are correct. Trailing padding is safe because the store never
# decodes the full image.


def _png(width: int = 1, height: int = 1, pad: int = 0) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    return sig + ihdr + iend + (b"\x00" * pad)


def _jpeg(width: int = 1, height: int = 1, pad: int = 0) -> bytes:
    soi = b"\xff\xd8"
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 11)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x01\x01\x11\x00"
    )
    eoi = b"\xff\xd9"
    return soi + app0 + sof0 + eoi + (b"\x00" * pad)


def _gif(width: int = 1, height: int = 1, pad: int = 0) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00" + (b"\x00" * pad)


def _webp(width: int = 1, height: int = 1, pad: int = 0) -> bytes:
    vp8_data = b"\x30\x00\x00\x9d\x01\x2a" + struct.pack("<HH", width & 0x3FFF, height & 0x3FFF)
    riff = b"RIFF" + struct.pack("<I", 4 + 4 + 8 + len(vp8_data)) + b"WEBP"
    vp8 = b"VP8 " + struct.pack("<I", len(vp8_data)) + vp8_data
    return riff + vp8 + (b"\x00" * pad)


_MIME_BYTES = {
    "image/png": _png,
    "image/jpeg": _jpeg,
    "image/gif": _gif,
    "image/webp": _webp,
}


@pytest.fixture
def state_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    wm_pkg = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(wm_pkg, "STATE_ROOT", tmp_path)
    return tmp_path


def _make_store(workspace_id: str = "ws1", session_id: str = "s1", **overrides):
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    return AgentStreamAttachmentStore(workspace_id, session_id, **overrides)


# ── config validation ───────────────────────────────────────────────────────


def test_rejects_non_positive_limits() -> None:
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    for kw in [
        {"max_preview_bytes": 0},
        {"max_preview_bytes": -1},
        {"max_session_count": 0},
        {"max_session_bytes": 0},
        {"max_global_count": 0},
        {"max_global_bytes": 0},
    ]:
        with pytest.raises(ValueError):
            AgentStreamAttachmentStore("ws", "s", **kw)


def test_rejects_inconsistent_limits() -> None:
    """A preview that fits max_preview_bytes must also fit the session and
    global byte budgets; otherwise a single save would be immediately
    self-evicted."""
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    # max_preview_bytes > max_session_bytes
    with pytest.raises(ValueError):
        AgentStreamAttachmentStore(
            "ws",
            "s",
            max_preview_bytes=1024,
            max_session_bytes=512,
            max_global_bytes=2048,
        )
    # max_preview_bytes > max_global_bytes
    with pytest.raises(ValueError):
        AgentStreamAttachmentStore(
            "ws",
            "s",
            max_preview_bytes=1024,
            max_session_bytes=2048,
            max_global_bytes=512,
        )


# ── per-file rejection ──────────────────────────────────────────────────────


def test_save_rejects_preview_over_per_file_byte_cap(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=1024)
    big = _png(pad=2048)

    with pytest.raises(ValueError):
        asyncio.run(store.save("image/png", big))


def test_save_accepts_preview_exactly_at_per_file_byte_cap(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=1024)
    base = _png()
    exact = base + b"\x00" * (1024 - len(base))

    meta = asyncio.run(store.save("image/png", exact))
    assert meta["bytes"] == 1024
    data, _ = asyncio.run(store.read(meta["id"]))
    assert data == exact


# ── MIME whitelist + magic bytes ────────────────────────────────────────────


def test_save_rejects_non_whitelisted_mime(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=10_000)
    with pytest.raises(ValueError):
        asyncio.run(store.save("image/bmp", _png()))
    with pytest.raises(ValueError):
        asyncio.run(store.save("text/html", b"<html></html>"))


def test_save_rejects_mime_spoof(state_root: Path) -> None:
    """Declared mime must match the magic bytes of the preview."""
    store = _make_store(max_preview_bytes=10_000)
    # Declare JPEG but supply PNG bytes.
    with pytest.raises(ValueError):
        asyncio.run(store.save("image/jpeg", _png()))
    # Declare PNG but supply JPEG bytes.
    with pytest.raises(ValueError):
        asyncio.run(store.save("image/png", _jpeg()))


def test_save_accepts_all_whitelisted_mimes(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=10_000)
    for mime, maker in _MIME_BYTES.items():
        meta = asyncio.run(store.save(mime, maker()))
        assert meta["mime_type"] == mime


# ── dimension enforcement ───────────────────────────────────────────────────


def test_save_rejects_oversized_dimensions(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=10_000)
    # Width > 1024.
    with pytest.raises(ValueError):
        asyncio.run(store.save("image/png", _png(width=2048, height=1)))
    # Height > 1024.
    with pytest.raises(ValueError):
        asyncio.run(store.save("image/png", _png(width=1, height=2048)))


def test_save_rejects_spoofed_dimensions(state_root: Path) -> None:
    """Client-supplied width/height must match the parsed header."""
    store = _make_store(max_preview_bytes=10_000)
    png = _png(width=100, height=50)
    with pytest.raises(ValueError):
        asyncio.run(store.save("image/png", png, width=200, height=50))
    with pytest.raises(ValueError):
        asyncio.run(store.save("image/png", png, width=100, height=99))


def test_save_returns_parsed_dimensions(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=10_000)
    meta = asyncio.run(store.save("image/png", _png(width=123, height=456)))
    assert meta["width"] == 123
    assert meta["height"] == 456


# ── session FIFO eviction ───────────────────────────────────────────────────


def test_session_fifo_eviction_when_count_exceeded(state_root: Path) -> None:
    store = _make_store(max_session_count=2, max_preview_bytes=10_000)

    async def run():
        a = await store.save("image/png", _png())
        b = await store.save("image/png", _png())
        c = await store.save("image/png", _png())
        return a, b, c

    a, b, c = asyncio.run(run())

    async def check():
        with pytest.raises(KeyError):
            await store.read(a["id"])
        await store.read(b["id"])
        await store.read(c["id"])

    asyncio.run(check())


def test_session_fifo_eviction_when_bytes_exceeded(state_root: Path) -> None:
    store = _make_store(max_session_bytes=250, max_preview_bytes=250)

    async def run():
        a = await store.save("image/png", _png(pad=55))  # 100 bytes
        b = await store.save("image/png", _png(pad=55))  # 100 bytes
        c = await store.save("image/png", _png(pad=55))  # 100 bytes
        return a, b, c

    a, b, c = asyncio.run(run())

    async def check():
        # 3 * 100 = 300 > 250 → oldest (a) evicted.
        with pytest.raises(KeyError):
            await store.read(a["id"])
        await store.read(b["id"])
        await store.read(c["id"])

    asyncio.run(check())


# ── global FIFO eviction (count and bytes) ──────────────────────────────────


def test_global_fifo_eviction_across_sessions_by_count(state_root: Path) -> None:
    store_a = _make_store(session_id="s1", max_global_count=2, max_preview_bytes=10_000)
    store_b = _make_store(session_id="s2", max_global_count=2, max_preview_bytes=10_000)

    async def run():
        a = await store_a.save("image/png", _png())
        b = await store_b.save("image/png", _png())
        c = await store_b.save("image/png", _png())
        return a, b, c

    a, b, c = asyncio.run(run())

    async def check():
        with pytest.raises(KeyError):
            await store_a.read(a["id"])
        await store_b.read(b["id"])
        await store_b.read(c["id"])

    asyncio.run(check())


def test_global_fifo_eviction_across_sessions_by_bytes(state_root: Path) -> None:
    store_a = _make_store(session_id="s1", max_global_bytes=250, max_preview_bytes=250)
    store_b = _make_store(session_id="s2", max_global_bytes=250, max_preview_bytes=250)

    async def run():
        a = await store_a.save("image/png", _png(pad=55))  # 100 bytes
        b = await store_b.save("image/png", _png(pad=55))  # 100 bytes
        c = await store_b.save("image/png", _png(pad=55))  # 100 bytes
        return a, b, c

    a, b, c = asyncio.run(run())

    async def check():
        # 3 * 100 = 300 > 250 → oldest (a) evicted.
        with pytest.raises(KeyError):
            await store_a.read(a["id"])
        await store_b.read(b["id"])
        await store_b.read(c["id"])

    asyncio.run(check())


# ── freshly inserted preview survival ───────────────────────────────────────


def test_freshly_saved_preview_is_not_self_evicted(state_root: Path) -> None:
    """A newly saved id must be readable immediately after save returns.

    With consistent limits (max_preview_bytes <= session/global bytes) a
    single preview can never be self-evicted. This test guards against a
    regression where save returns an id that was already evicted.
    """
    store = _make_store(
        max_preview_bytes=512,
        max_session_bytes=512,
        max_global_bytes=512,
        max_session_count=1,
        max_global_count=1,
    )
    base = _png()
    exact = base + b"\x00" * (512 - len(base))

    meta = asyncio.run(store.save("image/png", exact))
    # The id must still resolve.
    data, mime = asyncio.run(store.read(meta["id"]))
    assert data == exact
    assert mime == "image/png"


# ── concurrency / atomicity ─────────────────────────────────────────────────


def test_concurrent_sends_respect_session_count_limit(state_root: Path) -> None:
    store = _make_store(max_session_count=1, max_preview_bytes=10_000)

    async def run():
        results = await asyncio.gather(
            store.save("image/png", _png()),
            store.save("image/png", _png()),
            return_exceptions=True,
        )
        return results

    results = asyncio.run(run())
    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) >= 1

    session_dir = Path(store._session_dir)
    files = list(session_dir.glob("*")) if session_dir.exists() else []
    assert len(files) <= 1


# ── orphan cleanup (startup / recovery GC) ──────────────────────────────────


def test_gc_removes_orphan_files_not_in_index(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=10_000)

    async def run():
        a = await store.save("image/png", _png())
        orphan = Path(store._session_dir) / "orphan-abc123"
        orphan.write_bytes(b"junk")
        assert orphan.exists()
        await store.gc()
        return a, orphan

    a, orphan = asyncio.run(run())
    assert not orphan.exists()
    asyncio.run(store.read(a["id"]))


def test_gc_removes_unexpected_directory_named_like_attachment(
    state_root: Path,
) -> None:
    """Only regular files are valid preview objects. A corrupt directory at
    an attachment-id path must not survive GC or be served as an image.
    """
    store = _make_store(max_preview_bytes=10_000)
    meta = asyncio.run(store.save("image/png", _png()))
    path = Path(store._session_dir) / meta["id"]
    path.unlink()
    path.mkdir()
    (path / "nested-orphan").write_text("remove me")

    asyncio.run(store.gc())

    assert not path.exists()
    with pytest.raises(KeyError):
        asyncio.run(store.read(meta["id"]))


def test_gc_removes_stale_index_entries_for_missing_files(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=10_000)

    async def run():
        a = await store.save("image/png", _png())
        (Path(store._session_dir) / a["id"]).unlink()
        await store.gc()
        with pytest.raises(KeyError):
            await store.read(a["id"])

    asyncio.run(run())


def test_gc_enforces_global_quota_on_recovery(state_root: Path) -> None:
    """Startup GC must evict oldest-first if the persisted set exceeds the
    global quota (e.g. after a config change or a crash mid-eviction)."""
    store = _make_store(max_global_count=2, max_preview_bytes=10_000)

    async def run():
        a = await store.save("image/png", _png())
        b = await store.save("image/png", _png())
        c = await store.save("image/png", _png())
        from claude_hub.services.agent_stream.attachments import _index_path

        index = json.loads(_index_path().read_text())
        index.append(
            {
                "id": a["id"],
                "session_id": "s1",
                "workspace_id": "ws1",
                "mime_type": "image/png",
                "size": len(_png()),
                "width": 1,
                "height": 1,
                "created_at": 0.0,
            }
        )
        _index_path().write_text(json.dumps(index))
        (Path(store._session_dir) / a["id"]).write_bytes(_png())
        await store.gc()
        return a, b, c

    a, b, c = asyncio.run(run())

    async def check():
        with pytest.raises(KeyError):
            await store.read(a["id"])
        await store.read(b["id"])
        await store.read(c["id"])

    asyncio.run(check())


def test_gc_enforces_per_session_quota_on_recovery(state_root: Path) -> None:
    """Recovery GC must also enforce per-session quotas, not just global."""
    store = _make_store(max_session_count=1, max_preview_bytes=10_000)

    async def run():
        a = await store.save("image/png", _png())
        b = await store.save("image/png", _png())
        # b already evicted a; re-add a to the index to simulate a stale
        # over-quota session.
        from claude_hub.services.agent_stream.attachments import _index_path

        index = json.loads(_index_path().read_text())
        index.append(
            {
                "id": a["id"],
                "session_id": "s1",
                "workspace_id": "ws1",
                "mime_type": "image/png",
                "size": len(_png()),
                "width": 1,
                "height": 1,
                "created_at": 0.0,
            }
        )
        _index_path().write_text(json.dumps(index))
        (Path(store._session_dir) / a["id"]).write_bytes(_png())
        await store.gc()
        return a, b

    a, b = asyncio.run(run())

    async def check():
        # The session quota is 1; the oldest (a) must be evicted.
        with pytest.raises(KeyError):
            await store.read(a["id"])
        await store.read(b["id"])

    asyncio.run(check())


# ── path traversal / malicious index ────────────────────────────────────────


def test_read_rejects_path_traversal_ids(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=10_000)

    async def run():
        for bad in ["../etc/passwd", "..%2fetc%2fpasswd", "/etc/passwd", "a/b/c", "..\\..\\etc"]:
            with pytest.raises((KeyError, ValueError)):
                await store.read(bad)

    asyncio.run(run())


def test_workspace_and_session_ids_with_path_characters_are_hashed(state_root: Path) -> None:
    """workspace_id / session_id containing '.', '..', or separators must not
    influence the on-disk path — only their SHA-256 digests are used."""
    from claude_hub.services.agent_stream.attachments import _hash_component

    for bad_id in [".", "..", "../escape", "a/b", "a\\b", "/etc/passwd"]:
        store = _make_store(workspace_id=bad_id, session_id=bad_id)
        # The session directory must be the hash of the id, not the id itself.
        expected = (
            state_root
            / "agent_stream_attachment_previews"
            / _hash_component(bad_id)
            / _hash_component(bad_id)
        )
        assert Path(store._session_dir) == expected


def test_malicious_manifest_entry_cannot_touch_files_outside_preview_root(state_root: Path) -> None:
    """A corrupt index entry with malicious workspace/session ids must not
    cause gc/clear to delete or read files outside the dedicated preview root.

    We place a sentinel file elsewhere under STATE_ROOT and verify it survives
    gc even when the index references a path that would resolve to it under
    naive concatenation.
    """
    from claude_hub.services.agent_stream.attachments import _index_path

    store = _make_store(max_preview_bytes=10_000)
    # Save one legitimate preview.
    a = asyncio.run(store.save("image/png", _png()))

    # Place a sentinel file outside the preview root but inside STATE_ROOT.
    sentinel = state_root / "sentinel.txt"
    sentinel.write_text("do not touch")

    # Inject a malicious index entry whose raw ids, if concatenated naively,
    # would point at the sentinel. Because we hash the ids, the entry's path
    # resolves to a hash directory that does not contain the sentinel.
    index = json.loads(_index_path().read_text())
    index.append(
        {
            "id": a["id"],  # reuse a valid attachment id
            "session_id": "../../..",
            "workspace_id": "..",
            "mime_type": "image/png",
            "size": 1,
            "width": 1,
            "height": 1,
            "created_at": 0.0,
        }
    )
    _index_path().write_text(json.dumps(index))

    asyncio.run(store.gc())

    # The sentinel must be untouched.
    assert sentinel.exists()
    assert sentinel.read_text() == "do not touch"


# ── missing / evicted → 404/410 (no silent placeholder) ─────────────────────


def test_missing_attachment_returns_410_not_placeholder(state_root: Path) -> None:
    """A missing/evicted preview must NOT return a silent placeholder image.

    The API returns 410 Gone (valid id, not found) so the frontend can render
    a visible "Preview expired" placeholder via error state.
    """
    from fastapi import HTTPException

    from claude_hub.api.agent_stream import _read_attachment_or_404

    store = _make_store(max_preview_bytes=10_000)

    async def run():
        with pytest.raises(HTTPException) as exc_info:
            await _read_attachment_or_404(store, "0" * 32)
        return exc_info.value

    exc = asyncio.run(run())
    assert exc.status_code == 410
    # Must carry nosniff + no-store so the browser never caches the absence.
    assert exc.headers["X-Content-Type-Options"] == "nosniff"
    assert exc.headers["Cache-Control"] == "no-store"


def test_invalid_attachment_id_returns_404(state_root: Path) -> None:
    from fastapi import HTTPException

    from claude_hub.api.agent_stream import _read_attachment_or_404

    store = _make_store(max_preview_bytes=10_000)

    async def run():
        with pytest.raises(HTTPException) as exc_info:
            await _read_attachment_or_404(store, "not-a-uuid")
        return exc_info.value

    exc = asyncio.run(run())
    assert exc.status_code == 404
    assert exc.headers["X-Content-Type-Options"] == "nosniff"
    assert exc.headers["Cache-Control"] == "no-store"


def _data_url(mime_type: str, data: bytes) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def test_decode_accepts_independent_bounded_preview_mime() -> None:
    """A PNG original may use a JPEG preview; both MIME/magic pairs remain
    independently validated and only the preview is eligible for persistence.
    """
    from claude_hub.api.agent_stream import AgentStreamAttachment, _decode_attachments

    original = _png()
    preview = _jpeg()
    payload = AgentStreamAttachment(
        filename="diagram.png",
        mime_type="image/png",
        data_url=_data_url("image/png", original),
        preview_data_url=_data_url("image/jpeg", preview),
    )

    originals, previews = _decode_attachments([payload])

    assert originals == [original]
    assert previews == [preview]


def test_decode_rejects_mixed_preview_presence() -> None:
    from fastapi import HTTPException

    from claude_hub.api.agent_stream import AgentStreamAttachment, _decode_attachments

    with_preview = AgentStreamAttachment(
        filename="a.png",
        mime_type="image/png",
        data_url=_data_url("image/png", _png()),
        preview_data_url=_data_url("image/jpeg", _jpeg()),
    )
    without_preview = AgentStreamAttachment(
        filename="b.png",
        mime_type="image/png",
        data_url=_data_url("image/png", _png()),
    )

    with pytest.raises(HTTPException, match="all include previews") as exc_info:
        _decode_attachments([with_preview, without_preview])
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize(
    "data_url",
    [
        "data:image/png;base64,not-valid-base64!",
        "data:image/png,not-base64",
        "data:image/png;base64",
    ],
)
def test_decode_rejects_malformed_original_data_urls(data_url: str) -> None:
    from fastapi import HTTPException

    from claude_hub.api.agent_stream import AgentStreamAttachment, _decode_attachments

    payload = AgentStreamAttachment(
        filename="bad.png",
        mime_type="image/png",
        data_url=data_url,
    )
    with pytest.raises(HTTPException) as exc_info:
        _decode_attachments([payload])
    assert exc_info.value.status_code == 400


def test_decode_enforces_aggregate_original_and_preview_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    import claude_hub.api.agent_stream as api

    original = _png()
    preview = _jpeg()
    # Keep the fixture tiny while exercising the same aggregate accounting
    # branch as the production 40 MiB limit.
    monkeypatch.setattr(api, "_MAX_TOTAL_ATTACHMENT_BYTES", len(original) + len(preview) - 1)
    payload = api.AgentStreamAttachment(
        filename="too-much.png",
        mime_type="image/png",
        data_url=_data_url("image/png", original),
        preview_data_url=_data_url("image/jpeg", preview),
    )

    with pytest.raises(HTTPException, match="total attachment size") as exc_info:
        api._decode_attachments([payload])
    assert exc_info.value.status_code == 400


def test_attachment_read_is_scoped_to_workspace_and_session(state_root: Path) -> None:
    owner = _make_store("ws-owner", "session-owner", max_preview_bytes=10_000)
    other_session = _make_store("ws-owner", "session-other", max_preview_bytes=10_000)
    other_workspace = _make_store("ws-other", "session-owner", max_preview_bytes=10_000)
    meta = asyncio.run(owner.save("image/png", _png()))

    async def check() -> None:
        assert await owner.read(meta["id"]) == (_png(), "image/png")
        with pytest.raises(KeyError):
            await other_session.read(meta["id"])
        with pytest.raises(KeyError):
            await other_workspace.read(meta["id"])

    asyncio.run(check())


# ── original bytes are never persisted ───────────────────────────────────────


def test_only_preview_bytes_are_persisted_not_originals(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=10_000)
    preview = _png(pad=500)

    meta = asyncio.run(store.save("image/png", preview))
    on_disk = Path(store._session_dir) / meta["id"]
    assert on_disk.read_bytes() == preview
    assert on_disk.stat().st_size == len(preview)


# ── event payload contract ──────────────────────────────────────────────────


def test_save_returns_opaque_metadata_without_paths(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=10_000)

    meta = asyncio.run(store.save("image/png", _png(width=256, height=256)))
    assert "id" in meta
    assert meta["mime_type"] == "image/png"
    assert meta["bytes"] == len(_png(width=256, height=256))
    assert meta["width"] == 256
    assert meta["height"] == 256
    assert "path" not in meta
    assert "storage_key" not in meta
    assert not str(meta["id"]).startswith("/")
    assert ".." not in str(meta["id"])


# ── session deletion clears preview directory ────────────────────────────────


def test_clear_removes_all_session_previews(state_root: Path) -> None:
    store = _make_store(max_preview_bytes=10_000)

    async def run():
        a = await store.save("image/png", _png())
        b = await store.save("image/jpeg", _jpeg())
        await store.clear()
        return a, b

    a, b = asyncio.run(run())

    async def check():
        with pytest.raises(KeyError):
            await store.read(a["id"])
        with pytest.raises(KeyError):
            await store.read(b["id"])

    asyncio.run(check())
    assert not Path(store._session_dir).exists()


# ── integration: send path persists preview only, original bytes absent ──────


def test_send_path_persists_preview_only_not_original_bytes(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tailer's send_message must persist only the bounded preview bytes.
    The original (full-resolution) bytes must never appear under the preview
    root or in the turn_started event payload.
    """
    from datetime import datetime

    from claude_hub.models import (
        AgentStreamEventType,
        AgentType,
        ManagedSession,
        ManagedSessionStatus,
        SessionKind,
        WorkspaceSessionRole,
    )
    from claude_hub.services.agent_stream.tailer import SessionTailer

    session = ManagedSession(
        id="s1",
        workspace_id="ws1",
        tab_id="t1",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.WORKING,
        title="test",
        workspace_path="/tmp/ws",
        tmux_session="tmux-s1",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    original = _png(pad=5000)  # fake large original PNG
    preview = _jpeg(pad=500)  # small JPEG preview

    captured: Dict[str, Any] = {}

    class FakeTransport:
        _started = True
        turn_in_flight = False

        async def start(self):
            pass

        async def send_message(self, text, images):
            captured["provider_images"] = images

        async def stop(self):
            pass

    async def run():
        # Create the tailer inside the running loop so its asyncio.Lock()
        # instances bind to the correct loop (asyncio.run sets the current
        # loop to None on exit, which breaks constructor-time Lock creation).
        tailer = SessionTailer(
            workspace_id="ws1",
            session_id="s1",
            adapter=MagicMock(),
            session_getter=lambda: session,
            native_transport=FakeTransport(),
        )
        await tailer.send_message(
            "look at this",
            images=[original],
            previews=[preview],
            client_turn_id="turn-1",
        )
        return tailer

    tailer = asyncio.run(run())

    # The provider received the ORIGINAL bytes.
    assert captured["provider_images"] == [original]

    # The preview root must contain only the preview, never the original.
    preview_root = state_root / "agent_stream_attachment_previews"
    all_bytes = b""
    for f in preview_root.rglob("*"):
        if f.is_file():
            all_bytes += f.read_bytes()
    assert preview in all_bytes
    assert original not in all_bytes

    # The turn_started event payload must not contain original bytes or a
    # data URL — only opaque attachment metadata.
    events = asyncio.run(tailer.store.read_since(-1)).events
    turn_started = next(e for e in events if e.type == AgentStreamEventType.TURN_STARTED)
    atts = turn_started.payload.get("attachments", [])
    assert len(atts) == 1
    att = atts[0]
    assert "id" in att
    assert att["mime_type"] == "image/jpeg"
    assert att["bytes"] == len(preview)
    assert "data" not in att
    assert "path" not in att
    event_json = turn_started.model_dump_json()
    assert original.decode("latin-1") not in event_json


# ── TTL (max_age_seconds) validation: reject NaN / inf ───────────────────────


def test_rejects_non_finite_max_age_seconds() -> None:
    """NaN and inf TTLs must be rejected: inf would disable age eviction,
    NaN would make the age comparison behave unpredictably."""
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    for bad in [float("nan"), float("inf"), float("-inf")]:
        with pytest.raises(ValueError):
            AgentStreamAttachmentStore("ws", "s", max_age_seconds=bad)


def test_config_rejects_non_finite_attachment_max_age_seconds() -> None:
    """The settings validator must reject NaN/inf for attachment_max_age_seconds."""
    from claude_hub.config import Settings

    for bad in [float("nan"), float("inf"), float("-inf")]:
        with pytest.raises(ValueError):
            Settings(attachment_max_age_seconds=bad)


# ── TTL live enforcement in save (not only startup gc) ──────────────────────


def test_save_evicts_entries_older_than_max_age_seconds(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Age-based eviction must run during ``save`` (not only at startup gc).

    With ``max_age_seconds`` set, saving a new entry after the TTL has
    elapsed must evict the aged entry. We drive ``time.time`` with a fake
    clock so the test is deterministic and fast.
    """
    import claude_hub.services.agent_stream.attachments as att_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(att_mod.time, "time", lambda: clock["t"])

    store = _make_store(max_age_seconds=10, max_preview_bytes=10_000)

    async def run():
        a = await store.save("image/png", _png())
        # Advance past the TTL.
        clock["t"] += 11.0
        b = await store.save("image/png", _png())
        return a, b

    a, b = asyncio.run(run())

    async def check():
        # ``a`` is older than max_age_seconds → evicted during the second save.
        with pytest.raises(KeyError):
            await store.read(a["id"])
        # ``b`` is fresh → still present.
        await store.read(b["id"])

    asyncio.run(check())


def test_save_with_none_max_age_does_not_evict_by_age(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``max_age_seconds`` is ``None`` (the default), age-based eviction
    must be a no-op: no index scan, no extra manifest writes, and aged
    entries survive until quota eviction removes them."""
    import claude_hub.services.agent_stream.attachments as att_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(att_mod.time, "time", lambda: clock["t"])

    store = _make_store(max_age_seconds=None, max_preview_bytes=10_000)

    async def run():
        a = await store.save("image/png", _png())
        # Advance far past any plausible TTL.
        clock["t"] += 10_000.0
        b = await store.save("image/png", _png())
        return a, b

    a, b = asyncio.run(run())

    async def check():
        # No age eviction: both entries survive (quota is generous).
        await store.read(a["id"])
        await store.read(b["id"])

    asyncio.run(check())


def test_save_with_max_age_none_incurs_no_extra_index_write(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``max_age_seconds=None``, an under-quota save must write the
    manifest exactly once (publication only). ``_evict_aged`` must return
    immediately without scanning, so it contributes no extra write."""
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    original_write = AgentStreamAttachmentStore._write_index
    writes = {"n": 0}

    def counting_write(entries):
        writes["n"] += 1
        return original_write(entries)

    monkeypatch.setattr(AgentStreamAttachmentStore, "_write_index", staticmethod(counting_write))

    store = _make_store(max_age_seconds=None, max_preview_bytes=10_000)

    async def run():
        await store.save("image/png", _png())

    asyncio.run(run())

    # Publication write only; _evict_aged is a no-op so no post-eviction write.
    assert writes["n"] == 1


# ── symlink rejection: preview root itself ──────────────────────────────────


def test_gc_unlinks_symlinked_preview_root_without_touching_target(
    state_root: Path, tmp_path: Path
) -> None:
    """If the dedicated preview root is itself a symlink to an external
    directory, gc must unlink the symlink (never scandir the target) and
    clear the manifest. The external directory's contents must survive."""
    from claude_hub.services.agent_stream.attachments import (
        _attachments_root,
        _index_path,
    )

    # Create an external directory with a sentinel file.
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("do not delete me")

    # Replace the preview root with a symlink to the external directory.
    root = _attachments_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(external, root)
    assert root.is_symlink()

    # Seed a non-empty index so we can verify it gets cleared.
    _index_path().write_text(
        json.dumps(
            [
                {
                    "id": "a" * 32,
                    "workspace_id": "ws1",
                    "session_id": "s1",
                    "mime_type": "image/png",
                    "size": 1,
                    "width": 1,
                    "height": 1,
                    "created_at": 0.0,
                }
            ]
        )
    )

    store = _make_store(max_preview_bytes=10_000)
    asyncio.run(store.gc())

    # The symlink must be gone.
    assert not root.is_symlink()
    assert not root.exists()
    # The external sentinel must be untouched.
    assert sentinel.exists()
    assert sentinel.read_text() == "do not delete me"
    # The manifest must be cleared.
    assert json.loads(_index_path().read_text()) == []


def test_gc_handles_dangling_preview_root_symlink(state_root: Path, tmp_path: Path) -> None:
    """A dangling symlink at the preview root must be unlinked; gc must not
    raise and must clear the manifest."""
    from claude_hub.services.agent_stream.attachments import _attachments_root, _index_path

    missing = tmp_path / "does-not-exist"
    root = _attachments_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(missing, root)
    assert root.is_symlink()
    assert not root.exists()  # dangling

    _index_path().write_text(
        json.dumps(
            [
                {
                    "id": "a" * 32,
                    "workspace_id": "ws1",
                    "session_id": "s1",
                    "mime_type": "image/png",
                    "size": 1,
                    "width": 1,
                    "height": 1,
                    "created_at": 0.0,
                }
            ]
        )
    )

    store = _make_store(max_preview_bytes=10_000)
    asyncio.run(store.gc())

    assert not root.is_symlink()
    assert json.loads(_index_path().read_text()) == []


# ── symlink rejection: workspace / session hash components ──────────────────


def test_gc_drops_entries_whose_workspace_dir_is_a_symlink(
    state_root: Path, tmp_path: Path
) -> None:
    """A symlink at the workspace-hash component must not be followed: gc
    must drop the index entry and unlink the symlink, leaving the external
    sentinel untouched.

    The sentinel is placed at ``external/<sess_hash>/<att_id>`` — exactly
    where a naive symlink-following read would look for the attachment. If
    the code followed the workspace symlink, it would find (and potentially
    delete) this file. The test proves it does not.
    """
    from claude_hub.services.agent_stream.attachments import (
        _attachments_root,
        _hash_component,
        _index_path,
    )

    store = _make_store(max_preview_bytes=10_000)
    ws_hash = _hash_component("ws1")
    sess_hash = _hash_component("s1")
    att_id = "b" * 32

    # Create the real preview root.
    root = _attachments_root()
    root.mkdir(parents=True, exist_ok=True)

    # External target structured so that following the workspace symlink
    # would resolve the attachment path to external/sess_hash/att_id.
    external = tmp_path / "external-ws"
    (external / sess_hash).mkdir(parents=True)
    sentinel = external / sess_hash / att_id
    sentinel.write_text("keep me")

    # Replace the workspace hash dir with a symlink to the external dir.
    ws_dir = root / ws_hash
    os.symlink(external, ws_dir)

    # Seed an index entry for ws1/s1.
    _index_path().write_text(
        json.dumps(
            [
                {
                    "id": att_id,
                    "workspace_id": "ws1",
                    "session_id": "s1",
                    "mime_type": "image/png",
                    "size": 1,
                    "width": 1,
                    "height": 1,
                    "created_at": 0.0,
                }
            ]
        )
    )

    asyncio.run(store.gc())

    # The workspace symlink must be unlinked.
    assert not ws_dir.is_symlink()
    # The external sentinel must survive — gc must not have followed the
    # symlink to reach it.
    assert sentinel.exists()
    assert sentinel.read_text() == "keep me"
    # The index entry must be dropped (chain was not real).
    assert json.loads(_index_path().read_text()) == []


def test_gc_drops_entries_whose_session_dir_is_a_symlink(state_root: Path, tmp_path: Path) -> None:
    """A symlink at the session-hash component must not be followed. The
    sentinel sits at ``external/<att_id>`` (the path a symlink-following
    lookup would resolve to)."""
    from claude_hub.services.agent_stream.attachments import (
        _attachments_root,
        _hash_component,
        _index_path,
    )

    store = _make_store(max_preview_bytes=10_000)
    ws_hash = _hash_component("ws1")
    sess_hash = _hash_component("s1")
    att_id = "d" * 32

    root = _attachments_root()
    (root / ws_hash).mkdir(parents=True, exist_ok=True)

    external = tmp_path / "external-sess"
    external.mkdir()
    sentinel = external / att_id
    sentinel.write_text("keep me too")

    sess_dir = root / ws_hash / sess_hash
    os.symlink(external, sess_dir)

    _index_path().write_text(
        json.dumps(
            [
                {
                    "id": att_id,
                    "workspace_id": "ws1",
                    "session_id": "s1",
                    "mime_type": "image/png",
                    "size": 1,
                    "width": 1,
                    "height": 1,
                    "created_at": 0.0,
                }
            ]
        )
    )

    asyncio.run(store.gc())

    assert not sess_dir.is_symlink()
    assert sentinel.exists()
    assert sentinel.read_text() == "keep me too"
    assert json.loads(_index_path().read_text()) == []


def test_delete_entry_does_not_unlink_through_symlinked_workspace_dir(
    state_root: Path, tmp_path: Path
) -> None:
    """_delete_entry must refuse to operate on disk when the workspace/session
    ancestor chain contains a symlink — it must only drop the index entry,
    never unlink a file reached through the symlink.

    The sentinel is placed at ``external/<sess_hash>/<att_id>`` so that a
    naive ``path.unlink()`` through the workspace symlink would delete it.
    """
    from claude_hub.services.agent_stream.attachments import (
        _attachments_root,
        _hash_component,
        _index_path,
    )

    store = _make_store(max_preview_bytes=10_000)
    ws_hash = _hash_component("ws1")
    sess_hash = _hash_component("s1")
    att_id = "c" * 32

    root = _attachments_root()
    root.mkdir(parents=True, exist_ok=True)

    external = tmp_path / "external-sess"
    (external / sess_hash).mkdir(parents=True)
    sentinel = external / sess_hash / att_id
    sentinel.write_text("do not unlink through symlink")

    ws_dir = root / ws_hash
    os.symlink(external, ws_dir)

    _index_path().write_text(
        json.dumps(
            [
                {
                    "id": att_id,
                    "workspace_id": "ws1",
                    "session_id": "s1",
                    "mime_type": "image/png",
                    "size": 1,
                    "width": 1,
                    "height": 1,
                    "created_at": 0.0,
                }
            ]
        )
    )

    index = store._read_index()
    entry = index[0]
    store._delete_entry(index, entry, persist=True)

    # The external sentinel must NOT have been unlinked.
    assert sentinel.exists()
    assert sentinel.read_text() == "do not unlink through symlink"
    # The index entry must be dropped.
    assert json.loads(_index_path().read_text()) == []


# ── eviction performance: minimal manifest rewrites ─────────────────────────


def test_under_quota_save_writes_index_exactly_once(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save that does not trigger eviction must rewrite the manifest only
    once (the publication write). Eviction helpers must not self-persist."""
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    store = _make_store(max_preview_bytes=10_000)
    writes = {"n": 0}
    # _write_index is a @staticmethod; in Python 3, accessing it through the
    # class returns the underlying function directly.
    original_write = AgentStreamAttachmentStore._write_index

    def counting_write(entries):
        writes["n"] += 1
        return original_write(entries)

    monkeypatch.setattr(AgentStreamAttachmentStore, "_write_index", staticmethod(counting_write))

    asyncio.run(store.save("image/png", _png()))

    # Publication write only — eviction saw no victims and did not persist.
    assert writes["n"] == 1


def test_multi_victim_eviction_writes_once_not_per_victim(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a save triggers eviction of multiple victims, the manifest must
    be rewritten once for publication and once for the eviction result —
    never once per victim.

    We pre-fill 5 entries with a lenient store, then save a 6th with a
    strict ``max_session_count=1`` store for the same workspace/session.
    Eviction must remove 5 victims. Total writes for that save: 1 (pub) +
    1 (eviction result) = 2, regardless of victim count.
    """
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    # Lenient store: fill 5 entries in the same session.
    lenient = _make_store(max_session_count=100, max_preview_bytes=10_000)
    for _ in range(5):
        asyncio.run(lenient.save("image/png", _png()))

    # Strict store for the same workspace/session: cap=1.
    store = _make_store(max_session_count=1, max_preview_bytes=10_000)

    writes = {"n": 0}
    original_write = AgentStreamAttachmentStore._write_index

    def counting_write(entries):
        writes["n"] += 1
        return original_write(entries)

    monkeypatch.setattr(AgentStreamAttachmentStore, "_write_index", staticmethod(counting_write))

    # This save must evict 5 victims (all 5 pre-filled entries) to bring the
    # session count back to 1.
    asyncio.run(store.save("image/png", _png()))

    # 1 publication write + 1 post-eviction write = 2, not 1 (pub) + 5 (per
    # victim) = 6.
    assert writes["n"] == 2


# ── eviction failure: no net disk growth ────────────────────────────────────


def test_save_eviction_failure_does_not_grow_disk_usage(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If victim unlink fails during eviction, save must remove the newly
    saved entry+file and persist the remaining index. Repeated failed saves
    must not increase the on-disk file count or byte total beyond quota."""
    store = _make_store(max_session_count=1, max_preview_bytes=10_000)

    # Fill the session quota (count=1).
    first = asyncio.run(store.save("image/png", _png(pad=100)))

    # Make victim unlink raise (simulate permission / disk error).
    original_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        # Only fail for files inside the preview root (victims), not for
        # the temp-file cleanup inside _write_bytes_0600.
        try:
            rel = self.relative_to(state_root / "agent_stream_attachment_previews")
            # The first saved file is the victim; fail its unlink.
            if first["id"] in self.name:
                raise OSError("simulated unlink failure")
        except ValueError:
            pass
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    # Repeatedly attempt to save; each must fail (eviction error) and must
    # not leave the new preview on disk.
    for _ in range(5):
        with pytest.raises(OSError):
            asyncio.run(store.save("image/png", _png(pad=100)))

    # Count files under the preview root.
    preview_root = state_root / "agent_stream_attachment_previews"
    files = [f for f in preview_root.rglob("*") if f.is_file()]
    # Only the original (first) preview should remain; the failed saves'
    # previews were rolled back.
    assert len(files) == 1
    assert files[0].name == first["id"]
