"""Bounded text previews and the compatible local Markdown reader."""

import codecs
import os
import stat
import tempfile
from pathlib import Path
from typing import Collection

from claude_hub.models import ExecutionTarget, ManagedSession, WorkspaceArtifactPreview
from claude_hub.services.agent_stream.markdown_references import referenced_temporary_markdown

MARKDOWN_PREVIEW_MAX_BYTES = 512 * 1024
MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}


class MarkdownUnavailable(ValueError):
    """Opaque denial for missing, forbidden, or non-regular documents."""


class MarkdownEncodingError(ValueError):
    """The selected file is not UTF-8 text."""


def _read_beneath(root: Path, relative: Path) -> tuple[bytes, int]:
    # Open each component relative to a held directory descriptor. Reject
    # symlinks even if a directory changes after the containment check; never
    # follow a replacement outside the authorized root or block on a FIFO.
    directory = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in (*root.parts[1:], *relative.parts[:-1]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise MarkdownUnavailable()
            return handle.read(MARKDOWN_PREVIEW_MAX_BYTES + 1), info.st_size
    finally:
        os.close(directory)


def read_markdown_preview(session: ManagedSession, raw_path: str) -> WorkspaceArtifactPreview:
    """Read local UTF-8 Markdown without widening the session's file access."""
    return read_text_preview(session, raw_path, allowed_suffixes=MARKDOWN_SUFFIXES)


def read_text_preview(
    session: ManagedSession,
    raw_path: str,
    *,
    allowed_suffixes: Collection[str],
    allowed_names: Collection[str] = (),
) -> WorkspaceArtifactPreview:
    """Read an allowed local text format under the shared preview limits.

    An outside-cwd temporary file requires an exact reference in this Chat's
    persisted assistant output, using the same policy as assistant images.
    Remote sessions never resolve paths on the Hub host. Call in a worker thread.
    """
    if (
        session.target != ExecutionTarget.LOCAL
        or not session.workspace_path
        or not raw_path
        or len(raw_path) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in raw_path)
    ):
        raise MarkdownUnavailable()
    try:
        root = Path(session.workspace_path).expanduser().resolve(strict=True)
        path = Path(raw_path).expanduser()
        if path.suffix.lower() not in allowed_suffixes and path.name.lower() not in allowed_names:
            raise MarkdownUnavailable()
        path = Path(os.path.abspath(path if path.is_absolute() else root / path))
        if not path.is_relative_to(root):
            referenced = referenced_temporary_markdown(session, str(path))
            if referenced is None:
                raise MarkdownUnavailable()
            # Keep the open anchor at the OS temporary root so every user
            # directory is also opened without following symlinks.
            path = referenced
            roots = (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
            root = next(candidate for candidate in roots if path.is_relative_to(candidate))
        relative = path.relative_to(root)
        if not relative.parts or ".." in relative.parts:
            raise MarkdownUnavailable()
        data, size = _read_beneath(root, relative)
    except (OSError, RuntimeError, ValueError):
        raise MarkdownUnavailable() from None
    truncated = len(data) > MARKDOWN_PREVIEW_MAX_BYTES
    try:
        content = (
            codecs.getincrementaldecoder("utf-8-sig")().decode(
                data[:MARKDOWN_PREVIEW_MAX_BYTES], final=False
            )
            if truncated
            else data.decode("utf-8-sig")
        )
    except UnicodeError:
        raise MarkdownEncodingError() from None
    if "\x00" in content:
        raise MarkdownEncodingError()
    return WorkspaceArtifactPreview(
        path=str(path),
        filename=path.name,
        content=content,
        size_bytes=size,
        truncated=truncated,
    )
