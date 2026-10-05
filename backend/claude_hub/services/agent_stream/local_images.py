"""Exact-file grants for temporary images quoted by this tab's assistant.

The normal image reader is confined to cwd. Screenshots often live in the OS
temp directory instead: only an explicit Markdown image reference in the
canonical assistant stream grants access, never a user prompt or tool output.
No parent directory is added to the reader's allowed roots.
"""

from __future__ import annotations

import html
import json
import re
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import unquote

from ...models import ExecutionTarget, ManagedSession
from .store import AgentStreamStore

_TEMP_IMAGE_ROOTS = (Path(tempfile.gettempdir()), Path("/tmp"))
_IMAGE_REFERENCE = re.compile(
    r"!\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s<>]+?))" r"(?:\s+[\"'][^\n]*?[\"'])?\s*\)"
)


def _temporary_file(raw_path: str) -> Optional[Path]:
    candidate = Path(raw_path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None
    # /tmp is also a common explicit screenshot destination on macOS, where
    # tempfile.gettempdir() instead points into /var/folders.
    for root in _TEMP_IMAGE_ROOTS:
        for alias in {root, root.resolve()}:
            try:
                relative = candidate.relative_to(alias)
                real = candidate.resolve(strict=True)
                # Permit OS aliases (/tmp -> /private/tmp), but no symlink
                # files/directories below the temp root.
                if real == root.resolve() / relative and real.is_file():
                    return real
            except (OSError, ValueError, RuntimeError):
                continue
    return None


def referenced_temporary_image(session: ManagedSession, raw_path: str) -> Optional[Path]:
    """Return a single allowed real path, or None. Call from a worker thread."""
    if session.target != ExecutionTarget.LOCAL or not session.workspace_path:
        return None
    if not raw_path or len(raw_path) > 4096 or any(ord(ch) < 32 for ch in raw_path):
        return None
    try:
        real = _temporary_file(raw_path)
    except (OSError, ValueError, RuntimeError):
        return None
    if real is None:
        return None

    buffers: Dict[Tuple[str, str, str], str] = {}
    try:
        with AgentStreamStore(session.workspace_id, session.id).path.open() as events:
            for line in events:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict) or event.get("session_id") != session.id:
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict) or payload.get("role", "assistant") != "assistant":
                    continue
                message_id = str(event.get("message_id") or "")
                if message_id.endswith(":user"):
                    continue
                kind = event.get("type")
                if kind == "turn_completed":
                    text = payload.get("assistant_text")
                elif kind == "text_delta":
                    text = payload.get("text")
                else:
                    continue
                if not isinstance(text, str):
                    continue
                if kind == "text_delta":
                    key = (str(event.get("run_epoch")), str(event.get("turn_id")), message_id)
                    if payload.get("snapshot") is not True:
                        text = buffers.get(key, "") + text
                    # Preserve split destinations without retaining whole long
                    # conversations. No chunks from different messages join.
                    buffers[key] = text[-16384:]
                    if len(buffers) > 64:
                        del buffers[next(iter(buffers))]
                for match in _IMAGE_REFERENCE.finditer(text):
                    source = match.group(1) or match.group(2)
                    if unquote(html.unescape(source)) == raw_path:
                        return real
    except (OSError, UnicodeError):
        return None
    return None
