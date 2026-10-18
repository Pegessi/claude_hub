"""Image headers operations for attachment previews."""

from __future__ import annotations

from typing import Optional, Tuple

from .dependencies import MediaDependencies


def _magic_mime(data: bytes) -> Optional[str]:
    """Return the MIME type implied by the image's magic bytes, or ``None``."""
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _parse_image_dimensions(data: bytes, mime_type: str) -> Tuple[int, int]:
    """Parse ``(width, height)`` from a supported image's header.

    Raises ``ValueError`` if the header is malformed or the format is
    unsupported. Only the header bytes are inspected; the full image is never
    decoded.
    """
    if mime_type == "image/png":
        # 8-byte signature, then 4-byte length, 4-byte "IHDR", then
        # 4-byte width (BE) and 4-byte height (BE).
        if len(data) < 24:
            raise ValueError("PNG too short to contain IHDR")
        if data[12:16] != b"IHDR":
            raise ValueError("PNG missing IHDR chunk")
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height

    if mime_type == "image/gif":
        # 6-byte header ("GIF87a"/"GIF89a"), then 2-byte width (LE),
        # 2-byte height (LE).
        if len(data) < 10:
            raise ValueError("GIF too short to contain dimensions")
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return width, height

    if mime_type == "image/jpeg":
        # Scan for a SOF marker (0xFFC0..0xFFC3, excluding 0xFFC4/0xFFC8
        # which are DHT/DAC). The SOF segment layout is:
        #   0xFF 0xCn  segment_length(2 BE)  precision(1)  height(2 BE)  width(2 BE)
        i = 2  # skip the 0xFFD8 SOI marker
        while i + 9 < len(data):
            if data[i] != 0xFF:
                raise ValueError("JPEG marker byte not found at expected offset")
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xC3:
                # SOF0..SOF3: dimensions follow.
                height = int.from_bytes(data[i + 5 : i + 7], "big")
                width = int.from_bytes(data[i + 7 : i + 9], "big")
                return width, height
            # Skip the marker segment: length is at i+2 (2 bytes BE) and
            # includes the length field itself.
            seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
            i += 2 + seg_len
        raise ValueError("JPEG SOF marker not found")

    if mime_type == "image/webp":
        # RIFF header: "RIFF" size(4) "WEBP". Then a chunk with
        # "VP8 "/"VP8L"/"VP8X" fourcc.
        if len(data) < 30:
            raise ValueError("WebP too short to contain dimensions")
        fourcc = data[12:16]
        if fourcc == b"VP8 ":
            # Lossy: bytes 26-27 width (LE, mask 0x3FFF), 28-29 height.
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return width, height
        if fourcc == b"VP8L":
            # Lossless: byte 21 is 0x2F, then 4 bytes (LE) encode
            # width-1 (14 bits) and height-1 (14 bits).
            if data[20] != 0x2F:
                raise ValueError("WebP VP8L signature byte mismatch")
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height
        if fourcc == b"VP8X":
            # Extended: bytes 24-27 width (24-bit LE), 27-30 height.
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        raise ValueError(f"unsupported WebP chunk: {fourcc!r}")

    raise ValueError(f"cannot parse dimensions for unsupported mime {mime_type!r}")


def _validate_preview_mime(deps: MediaDependencies, mime_type: str, data: bytes) -> None:
    """Reject non-whitelisted MIME types and magic-byte / declared-MIME mismatches."""
    if mime_type not in deps._ALLOWED_MIME_TYPES:
        raise ValueError(
            f"unsupported mime type {mime_type!r}; allowed: {sorted(deps._ALLOWED_MIME_TYPES)}"
        )
    detected = deps._magic_mime(data)
    if detected is None:
        raise ValueError("preview bytes are not a recognized image (PNG/JPEG/GIF/WebP)")
    if detected != mime_type:
        raise ValueError(
            f"mime mismatch: declared {mime_type!r} but magic bytes indicate {detected!r}"
        )


def preview_within_cache_bounds(
    deps: MediaDependencies,
    mime_type: str,
    preview_bytes: bytes,
    *,
    max_preview_bytes: Optional[int] = None,
) -> bool:
    """Return whether an image may be persisted by the preview cache.

    External ingress paths use this before marking an original as its own
    preview.  The store remains the final enforcement point; this predicate
    only lets callers degrade an uncacheable original to a non-renderable
    placeholder without rejecting the original image sent to the provider.
    """

    if max_preview_bytes is None:
        max_preview_bytes = deps._store_kwargs_from_settings()["max_preview_bytes"]
    if len(preview_bytes) > max_preview_bytes:
        return False
    try:
        deps._validate_preview_mime(mime_type, preview_bytes)
        width, height = deps._parse_image_dimensions(preview_bytes, mime_type)
    except ValueError:
        return False
    return 1 <= width <= deps.MAX_PREVIEW_EDGE and 1 <= height <= deps.MAX_PREVIEW_EDGE
