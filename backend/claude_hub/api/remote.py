import asyncio
import base64
import json
import shlex
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..api.filesystem import DirectoryListing
from ..auth.dependencies import get_current_user
from ..models import RemoteProfile, User
from ..services import remote_profile_manager
from ..services.remote_profiles import profile_uses_stdin_shell

router = APIRouter(prefix="/api/remote", tags=["remote"])

REMOTE_LIST_SCRIPT = r"""
import json
import os
import sys

path = os.path.expandvars(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~"))
path = os.path.abspath(path)
if not os.path.exists(path):
    print(json.dumps({"error": "Path not found", "status": 404}))
    raise SystemExit(0)
if not os.path.isdir(path):
    print(json.dumps({"error": "Not a directory", "status": 400}))
    raise SystemExit(0)

items = []
try:
    with os.scandir(path) as entries:
        for entry in entries:
            try:
                items.append({
                    "name": entry.name,
                    "path": entry.path,
                    "is_dir": entry.is_dir(follow_symlinks=True),
                    "is_symlink": entry.is_symlink(),
                })
            except OSError:
                pass
except PermissionError:
    print(json.dumps({"error": "Permission denied", "status": 403}))
    raise SystemExit(0)

items.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
parent = os.path.dirname(path) if os.path.dirname(path) != path else None
print(json.dumps({
    "current_path": path,
    "parent_path": parent,
    "items": items,
}))
"""


def _ssh_target(profile: RemoteProfile) -> str:
    return f"{profile.user}@{profile.ssh_host}" if profile.user else profile.ssh_host


def remote_listing_command(remote_path: str, *, stdin_shell: bool) -> str:
    """Build the remote python listing command.

    Stdin-shell aliases execute one line from stdin. Keep that payload on a
    single physical line by wrapping the listing script in base64.
    """
    if stdin_shell:
        encoded = base64.b64encode(REMOTE_LIST_SCRIPT.encode("utf-8")).decode("ascii")
        runner = "import base64,sys; " f"exec(base64.b64decode({encoded!r}).decode());"
        return f"python3 -c {shlex.quote(runner)} {shlex.quote(remote_path)}"
    return f"python3 -c {shlex.quote(REMOTE_LIST_SCRIPT)} {shlex.quote(remote_path)}"


def parse_remote_listing_stdout(raw: str) -> dict:
    """Parse directory JSON even when SSH MOTD/banners precede it."""
    decoder = json.JSONDecoder()
    text = raw.strip()
    chosen: dict | None = None
    idx = 0
    while idx < len(text):
        brace = text.find("{", idx)
        if brace < 0:
            break
        try:
            payload, consumed = decoder.raw_decode(text[brace:])
        except json.JSONDecodeError:
            idx = brace + 1
            continue
        if isinstance(payload, dict) and (
            "current_path" in payload or "items" in payload or "error" in payload
        ):
            chosen = payload
        idx = brace + max(consumed, 1)
    if chosen is None:
        raise json.JSONDecodeError("Remote returned invalid directory data", text, 0)
    return chosen


@router.get("/profiles", response_model=List[RemoteProfile])
async def list_remote_profiles(
    current_user: User = Depends(get_current_user),
) -> list[RemoteProfile]:
    return remote_profile_manager.list_profiles()


@router.get("/filesystem/list", response_model=DirectoryListing)
async def list_remote_directory(
    profile_id: str = Query(...),
    path: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
) -> DirectoryListing:
    profile = remote_profile_manager.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Remote profile not found")

    remote_path = path or profile.default_cwd or "~"
    stdin_shell = profile_uses_stdin_shell(profile)
    remote_command = remote_listing_command(remote_path, stdin_shell=stdin_shell)
    cmd = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "RequestTTY=no",
    ]
    if profile.port != 22:
        cmd.extend(["-p", str(profile.port)])
    cmd.append(_ssh_target(profile))
    stdin_bytes: bytes | None = None
    if stdin_shell:
        stdin_bytes = (remote_command + "\n").encode()
    else:
        cmd.append(remote_command)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes),
            timeout=20 if stdin_shell else 8,
        )
    except asyncio.TimeoutError as e:
        raise HTTPException(status_code=504, detail="Remote directory listing timed out") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list remote directory: {e}") from e

    if proc.returncode != 0:
        error = stderr.decode("utf-8", errors="ignore").strip()
        raise HTTPException(status_code=502, detail=error or "Remote directory listing failed")

    try:
        payload = parse_remote_listing_stdout(stdout.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail="Remote returned invalid directory data") from e

    if "error" in payload:
        raise HTTPException(
            status_code=int(payload.get("status", 500)),
            detail=str(payload["error"]),
        )

    return DirectoryListing(**payload)
