"""Read-only local MASO discovery for startup and admission of new Chats.

Registry entries are hints, never readiness evidence. Only loopback ports are
used; public export URLs are deliberately ignored. Existing Chats bypass this
discovery and retain their persisted service identity.
"""

import asyncio
import json
import os
from pathlib import Path

from ..models import StreamModelOption
from .agent_stream import maso_http

MAX_REGISTRY_BYTES = 64 * 1024
MAX_CANDIDATES = 16


def local_maso_candidates() -> list[str]:
    candidates = {maso_http.DEFAULT_MASO_BASE_URL}
    try:
        with (Path.home() / ".maso" / "instances.json").open("rb") as source:
            raw = source.read(MAX_REGISTRY_BYTES + 1)
        if len(raw) > MAX_REGISTRY_BYTES:
            raise maso_http.MasoServiceUnavailable(
                "MASO instance registry is too large. Set MASO_BASE_URL explicitly, then retry."
            )
        rows = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        # Older installations have no registry. Keep their standard endpoint.
        return sorted(candidates)
    if isinstance(rows, dict):
        for row in rows.values():
            port = row.get("port") if isinstance(row, dict) else None
            if type(port) is int and 1 <= port <= 65535:
                candidates.add(f"http://127.0.0.1:{port}")
    if len(candidates) > MAX_CANDIDATES:
        raise maso_http.MasoServiceUnavailable(
            "Too many local MASO instances. Set MASO_BASE_URL explicitly, then retry."
        )
    return sorted(candidates)


async def discover_maso_service(
    env: dict[str, str],
) -> tuple[str, list[StreamModelOption], str]:
    """Validate an explicit target, otherwise require one usable local target."""
    if maso_http.configured_maso_base_url(env) or os.environ.get("MASO_PORT"):
        base_url = maso_http.maso_base_url(env)
        models, default = await maso_http.check_maso_service({"MASO_BASE_URL": base_url})
        return base_url, models, default

    async def probe(base_url: str) -> tuple[str, list[StreamModelOption], str] | None:
        try:
            models, default = await maso_http.check_maso_service({"MASO_BASE_URL": base_url})
        except maso_http.MasoServiceUnavailable:
            return None
        return base_url, models, default

    candidates = local_maso_candidates()
    # Concurrent probes share one deadline: many stale registrations cannot
    # multiply startup latency. Cancellation settles all probe tasks.
    try:
        async with asyncio.timeout(3.5):
            results = await asyncio.gather(*(probe(url) for url in candidates))
    except TimeoutError:
        raise maso_http.MasoServiceUnavailable(
            "Local MASO discovery timed out. Set MASO_BASE_URL explicitly, then retry."
        ) from None
    available = [result for result in results if result is not None]
    if len(available) == 1:
        return available[0]
    if available:
        urls = ", ".join(result[0] for result in available)
        raise maso_http.MasoServiceUnavailable(
            f"Multiple local MASO services are ready ({urls}). "
            "Set MASO_BASE_URL to the intended service, then retry."
        )
    raise maso_http.MasoServiceUnavailable(
        "MASO service is unavailable: no usable local service was found. "
        "Start MASO or set MASO_BASE_URL, then retry."
    )
