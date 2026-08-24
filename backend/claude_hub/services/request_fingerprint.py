"""Stable request fingerprints for call_id idempotency."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict


def request_fingerprint(action: str, payload: Dict[str, Any]) -> str:
    """Compute a stable fingerprint of a full action request.

    The fingerprint covers the action name and every request field so that a
    reused ``call_id`` carrying a different payload is detected and rejected.
    """

    canonical = json.dumps({"action": action, **payload}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
