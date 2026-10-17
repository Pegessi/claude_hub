"""Image validation and private, runtime-owned temporary file operations.

Codex/TraeX send localImage paths; Cursor references paths in its prompt. Files
are scoped to the owning runtime, never the durable workspace store. Sessions
retain turn ownership and delete only their staged or in-flight paths.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional


def detect_image_mime(data: bytes, *, max_image_bytes: int) -> Optional[str]:
    """Reject oversized or unrecognized images before they enter a provider turn."""
    if not data or len(data) > max_image_bytes:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return None


def mime_extension(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(mime, ".bin")


def image_temp_dir(runtime_home: Callable[[], Path], name: str) -> Path:
    """Create a runtime-private directory, defeating inherited umask settings."""
    path = runtime_home() / "tmp" / name
    path.mkdir(parents=True, exist_ok=True)
    current = path
    root = runtime_home()
    while current != root and current != current.parent:
        try:
            os.chmod(current, 0o700)
        except OSError:
            pass
        current = current.parent
    return path


def cleanup_image_temp_dir(temp_dir: Path, max_age_seconds: Optional[float]) -> int:
    """Remove leftover files after the caller proves ownership of this runtime.

    The startup instance lock permits deleting all prior-process files. An age
    bound can instead be supplied for an in-process safety-net cleanup.
    """
    now = time.time()
    removed = 0
    for entry in temp_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            if max_age_seconds is None or (now - entry.stat().st_mtime) > max_age_seconds:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    return removed
