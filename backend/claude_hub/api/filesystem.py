import os
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.dependencies import get_current_user
from ..models import User

router = APIRouter(prefix="/api/filesystem", tags=["filesystem"])


class FileInfo(BaseModel):
    name: str
    path: str
    is_dir: bool
    is_symlink: bool


class DirectoryListing(BaseModel):
    current_path: str
    parent_path: Optional[str]
    items: List[FileInfo]


def normalize_path(path: str) -> Path:
    """Normalize a path, expanding ~ and user variables."""
    expanded = os.path.expanduser(path)
    expanded = os.path.expandvars(expanded)
    return Path(expanded).resolve()


def safe_list_dir(path: str) -> DirectoryListing:
    """Safely list directory contents, preventing path traversal."""
    try:
        target_path = normalize_path(path)

        if not target_path.exists():
            raise HTTPException(status_code=404, detail="Path not found")

        if not target_path.is_dir():
            raise HTTPException(status_code=400, detail="Not a directory")

        # Get parent path
        parent_path = None
        if target_path.parent != target_path:
            parent_path = str(target_path.parent)

        items: List[FileInfo] = []

        # List directory contents
        for entry in target_path.iterdir():
            try:
                items.append(
                    FileInfo(
                        name=entry.name,
                        path=str(entry),
                        is_dir=entry.is_dir(),
                        is_symlink=entry.is_symlink(),
                    )
                )
            except (OSError, PermissionError):
                # Skip entries we can't access
                continue

        # Sort: directories first, then files, both alphabetically
        items.sort(key=lambda x: (not x.is_dir, x.name.lower()))

        return DirectoryListing(
            current_path=str(target_path),
            parent_path=parent_path,
            items=items,
        )

    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list", response_model=DirectoryListing)
async def list_directory(
    path: Optional[str] = None,
    current_user: User = Depends(get_current_user),
) -> DirectoryListing:
    """List contents of a directory. Defaults to user's home."""
    if path is None:
        path = "~"
    return safe_list_dir(path)


@router.get("/home")
async def get_home_directory(
    current_user: User = Depends(get_current_user),
) -> str:
    """Get the user's home directory path."""
    return str(Path.home())
