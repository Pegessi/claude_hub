"""Manifest normalization and atomic persistence for attachment previews."""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .dependencies import MediaDependencies


def _normalize_entry(deps: MediaDependencies, entry: Any) -> Optional[Dict[str, Any]]:
    """Validate and normalize a manifest entry.

    Returns a cleaned dict with all required fields of the correct type and
    range, or ``None`` if the entry is malformed. Malformed entries are
    dropped before they can affect eviction math (``size``/``created_at``)
    or serving (``mime_type``/``width``/``height``).
    """
    if not isinstance(entry, dict):
        return None

    att_id = entry.get("id")
    ws = entry.get("workspace_id")
    sess = entry.get("session_id")
    mime = entry.get("mime_type")
    size = entry.get("size")
    width = entry.get("width")
    height = entry.get("height")
    created_at = entry.get("created_at")

    if not isinstance(att_id, str):
        return None
    try:
        uuid.UUID(att_id)
    except (ValueError, AttributeError, TypeError):
        return None

    if not isinstance(ws, str) or not ws:
        return None
    if not isinstance(sess, str) or not sess:
        return None
    if not isinstance(mime, str) or mime not in deps._ALLOWED_MIME_TYPES:
        return None
    # ``size`` must be a positive int (not bool, not float).
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or not (1 <= width <= deps.MAX_PREVIEW_EDGE)
    ):
        return None
    if (
        isinstance(height, bool)
        or not isinstance(height, int)
        or not (1 <= height <= deps.MAX_PREVIEW_EDGE)
    ):
        return None
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        return None
    # Reject NaN, inf, and negative timestamps so LRU sorting and TTL
    # eviction cannot be poisoned by a corrupt manifest entry.
    if not math.isfinite(created_at) or created_at < 0:
        return None
    last_used_at = entry.get("last_used_at", created_at)
    if (
        isinstance(last_used_at, bool)
        or not isinstance(last_used_at, (int, float))
        or not math.isfinite(last_used_at)
        or last_used_at < 0
    ):
        last_used_at = created_at

    normalized = {
        "id": att_id,
        "workspace_id": ws,
        "session_id": sess,
        "mime_type": mime,
        "size": size,
        "width": width,
        "height": height,
        "created_at": float(created_at),
        "last_used_at": float(last_used_at),
    }
    # Agent outputs share this manifest and its quotas. Only hashes are kept;
    # a source filename never becomes a manifest-controlled filesystem path.
    if "source_key" in entry:
        for field in ("source_key", "source_digest"):
            value = entry.get(field)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                return None
            normalized[field] = value
    return normalized


def _lru_key(entry: Dict[str, Any]) -> Tuple[float, float]:
    return (entry.get("last_used_at", entry["created_at"]), entry["created_at"])


def _read_index(deps: MediaDependencies, *, strict: bool = False) -> List[Dict[str, Any]]:
    path = deps._index_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        if strict:
            raise
        return []
    if not isinstance(data, list):
        if strict:
            raise ValueError("invalid attachment index")
        return []
    normalized: List[Dict[str, Any]] = []
    for raw in data:
        entry = deps._normalize_entry(raw)
        if entry is not None:
            normalized.append(entry)
        elif strict:
            raise ValueError("invalid attachment index entry")
    return normalized


def _write_index(deps: MediaDependencies, entries: List[Dict[str, Any]]) -> None:
    path = deps._index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 448)
    except OSError:
        pass
    deps._write_bytes_0600(path, json.dumps(entries).encode("utf-8"))
