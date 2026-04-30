import asyncio
import json
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
MAX_IMAGE_BYTES = 25 * 1024 * 1024


class ClipboardImageResponse(BaseModel):
    path: str
    content_type: str
    size: int


def _detect_image_type(data: bytes, content_type: str | None) -> tuple[str, str, str]:
    normalized = (content_type or "").split(";")[0].strip().lower()

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png", "«class PNGf»"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg", "JPEG picture"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif", "GIF picture"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff", ".tiff", "TIFF picture"

    if normalized == "image/png":
        return "image/png", ".png", "«class PNGf»"
    if normalized in {"image/jpeg", "image/jpg"}:
        return "image/jpeg", ".jpg", "JPEG picture"
    if normalized == "image/gif":
        return "image/gif", ".gif", "GIF picture"
    if normalized in {"image/tiff", "image/tif"}:
        return "image/tiff", ".tiff", "TIFF picture"

    raise HTTPException(status_code=415, detail="Unsupported clipboard image type")


async def _set_macos_clipboard_image(path: Path, applescript_type: str) -> None:
    if platform.system() != "Darwin":
        raise HTTPException(
            status_code=501,
            detail="Setting image clipboard data is only supported on macOS",
        )

    script = (
        "set the clipboard to " f"(read (POSIX file {json.dumps(str(path))}) as {applescript_type})"
    )
    proc = await asyncio.create_subprocess_exec(
        "osascript",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = (stderr or stdout).decode("utf-8", errors="ignore").strip()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to set macOS clipboard image: {detail or proc.returncode}",
        )


@router.post("/image", response_model=ClipboardImageResponse)
async def upload_clipboard_image(
    image: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> ClipboardImageResponse:
    """Persist a pasted browser image and put it on the macOS clipboard for Codex TUI."""
    data = await image.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Clipboard image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Clipboard image is too large")

    content_type, suffix, applescript_type = _detect_image_type(data, image.content_type)
    CLIPBOARD_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"clipboard-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}{suffix}"
    path = CLIPBOARD_IMAGE_DIR / filename
    path.write_bytes(data)

    await _set_macos_clipboard_image(path, applescript_type)
    return ClipboardImageResponse(path=str(path), content_type=content_type, size=len(data))
