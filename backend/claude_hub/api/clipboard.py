import asyncio
import platform
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..auth.dependencies import get_current_user
from ..models import User

router = APIRouter(prefix="/api/clipboard", tags=["clipboard"])

CLIPBOARD_IMAGE_DIR = Path.home() / ".claude_hub" / "clipboard-images"
CLIPBOARD_HELPER_DIR = Path.home() / ".claude_hub" / "bin"
CLIPBOARD_HELPER_BINARY = CLIPBOARD_HELPER_DIR / "clipboard_set_image"
CLIPBOARD_HELPER_SOURCE = Path(__file__).with_name("clipboard_set_image.swift")
MAX_IMAGE_BYTES = 25 * 1024 * 1024
_HELPER_BUILD_LOCK = asyncio.Lock()


class ClipboardImageResponse(BaseModel):
    path: str
    content_type: str
    size: int
    pasteboard_synced: bool
    pasteboard_error: str | None = None


def _detect_image_type(data: bytes, content_type: str | None) -> tuple[str, str, str]:
    normalized = (content_type or "").split(";")[0].strip().lower()

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png", "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg", "JPEG"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif", "GIF"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff", ".tiff", "TIFF"

    if normalized == "image/png":
        return "image/png", ".png", "PNG"
    if normalized in {"image/jpeg", "image/jpg"}:
        return "image/jpeg", ".jpg", "JPEG"
    if normalized == "image/gif":
        return "image/gif", ".gif", "GIF"
    if normalized in {"image/tiff", "image/tif"}:
        return "image/tiff", ".tiff", "TIFF"

    raise HTTPException(status_code=415, detail="Unsupported clipboard image type")


def _helper_binary_is_fresh() -> bool:
    """True if the compiled Swift helper exists and is newer than its source."""
    if not CLIPBOARD_HELPER_BINARY.exists():
        return False
    try:
        return CLIPBOARD_HELPER_BINARY.stat().st_mtime >= CLIPBOARD_HELPER_SOURCE.stat().st_mtime
    except FileNotFoundError:
        return False


async def _ensure_helper_binary() -> None:
    """Lazily compile the Swift NSPasteboard helper.

    AppleScript and JXA both have failure modes in a launchd-descended backend
    (StandardAdditions not loaded, TCC gating for `read POSIX file`, JXA bridge
    instability on `setDataForType` inside uvicorn workers). A native Swift
    binary calls NSPasteboard directly with no scripting-bridge fragility.
    Compile it on first use and cache the result.
    """
    if _helper_binary_is_fresh():
        return
    async with _HELPER_BUILD_LOCK:
        if _helper_binary_is_fresh():
            return
        CLIPBOARD_HELPER_DIR.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "swiftc",
            "-O",
            str(CLIPBOARD_HELPER_SOURCE),
            "-o",
            str(CLIPBOARD_HELPER_BINARY),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="ignore").strip()
            raise HTTPException(
                status_code=500,
                detail=f"Failed to compile clipboard helper: {detail or proc.returncode}",
            )


async def _set_macos_clipboard_image(path: Path, pasteboard_type: str) -> tuple[bool, str | None]:
    """Try to put `path` on the macOS general pasteboard.

    Returns (synced, error_detail). Failures are not fatal: the file on disk
    is still useful to agents like Cursor that accept image attachments by
    absolute path typed at the prompt. Only Claude/Codex actually rely on
    the pasteboard sync (they re-read the system clipboard on Ctrl+V).

    Known failure modes when the backend is launched as a launchd-descended
    (daemonized) process — start.sh detached from its parent shell:
      * AppleScript route hits StandardAdditions / TCC blocks for `read POSIX
        file`.
      * Even a native Swift binary's `NSPasteboard.setData` returns false
        because the responsible application of a launchd-descended process
        is not the active GUI user session and macOS denies pasteboard
        writes.
    To make Claude/Codex paste-image actually work, the backend must be
    started from an attached GUI Terminal (so the process tree's responsible
    app is Terminal/iTerm), e.g. by running ./start.sh in a foreground
    terminal window.
    """
    if platform.system() != "Darwin":
        return False, "Setting image clipboard data is only supported on macOS"

    try:
        await _ensure_helper_binary()
    except HTTPException as exc:
        return False, str(exc.detail)

    proc = await asyncio.create_subprocess_exec(
        str(CLIPBOARD_HELPER_BINARY),
        str(path),
        pasteboard_type,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode("utf-8", errors="ignore").strip()
    if proc.returncode == 0 and output == "OK":
        return True, None
    detail = output or stderr.decode("utf-8", errors="ignore").strip() or str(proc.returncode)
    return False, detail


@router.post("/image", response_model=ClipboardImageResponse)
async def upload_clipboard_image(
    image: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> ClipboardImageResponse:
    """Persist a pasted browser image and best-effort sync it to the macOS pasteboard.

    The persisted absolute path is always returned. Whether the macOS
    pasteboard sync succeeded is reported via ``pasteboard_synced`` so the
    frontend can choose the right paste protocol per agent:
      * Cursor agent doesn't need pasteboard sync — it just types the path.
      * Claude / Codex need pasteboard sync — they read it on Ctrl+V.
    """
    data = await image.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Clipboard image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Clipboard image is too large")

    content_type, suffix, pasteboard_type = _detect_image_type(data, image.content_type)
    CLIPBOARD_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"clipboard-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}{suffix}"
    path = CLIPBOARD_IMAGE_DIR / filename
    path.write_bytes(data)

    pasteboard_synced, pasteboard_error = await _set_macos_clipboard_image(path, pasteboard_type)
    return ClipboardImageResponse(
        path=str(path),
        content_type=content_type,
        size=len(data),
        pasteboard_synced=pasteboard_synced,
        pasteboard_error=pasteboard_error,
    )
