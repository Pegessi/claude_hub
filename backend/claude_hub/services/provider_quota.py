"""Read-only remaining-quota readings for the Codex and MASO providers.

Two independent sources, one shared vocabulary. Codex publishes no API, so its
reading is salvaged from the newest ``token_count`` record in the local rollout
files it writes per turn; MASO owns its own quota accounting and is asked over
HTTP. Neither path creates a session, mutates provider state, or decides
anything: this module only reports what each provider last said, plus why a
reading is missing. A provider that cannot be read degrades to an unavailable
reading with an actionable message; it never raises at the caller.
"""

from __future__ import annotations

import glob
import heapq
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..models import ProviderQuota, QuotaLimit, QuotaResponse
from .agent_stream.maso_http import MasoServiceUnavailable, configured_maso_base_url
from .maso_discovery import discover_maso_service, local_maso_registry_ports

logger = logging.getLogger(__name__)

# Provider-neutral state vocabulary shared with the UI. Only MASO reports
# "no_limit"/"no_data" per limit; "unavailable" and "not_configured" are
# provider-level states Hub derives and never accepts from upstream.
_LIMIT_STATES = frozenset({"within", "near", "over", "no_limit", "no_data", "unknown"})
# Provider badge severity. "unknown" outranks "within" so a reading we could
# not fully confirm is never presented as a clean bill of health.
_STATE_SEVERITY = {"within": 0, "no_limit": 0, "no_data": 1, "unknown": 2, "near": 3, "over": 4}

# Hub-defined thresholds. Codex reports a raw percentage and no state, so Hub
# derives one. MASO computes its own near/over and is never re-thresholded
# here, because its bands account for grants Hub cannot see.
CODEX_NEAR_PERCENT = 80
CODEX_OVER_PERCENT = 100

# Bounds for the rollout scan. Rate-limit records are written at the end of
# every turn, so the tail of a file is enough; roughly one file in five (an
# aborted session) carries none at all, hence the small cross-file fallback.
_CODEX_TAIL_BYTES = 64 * 1024
_CODEX_MAX_FILES_READ = 8
# Opening file tails is the expensive step and is already bounded above. Stat
# is cheap, so the scan stats every rollout and retains only the newest
# candidates by mtime; the scan budget is a runaway guard, never the rule that
# decides which file is newest.
_CODEX_MAX_CANDIDATES = 64
_CODEX_MAX_FILES_VISITED = 20000

# MASO already caches this reading for ~60s server-side; the Hub-side TTL only
# keeps a shared panel from re-running service discovery on every poll.
_QUOTA_CACHE_TTL = 30.0
_MASO_TIMEOUT_S = 3.0

_CODEX_NO_DATA_MESSAGE = (
    "No Codex rate-limit reading was found on this host. "
    "Run or resume a Codex session, then refresh."
)
_CODEX_UNREADABLE_MESSAGE = (
    "Codex session files could not be read on this host. "
    "Check the permissions on the Codex home directory, then refresh."
)
_MASO_UNAVAILABLE_MESSAGE = "MASO quota is unavailable. Check the MASO service, then refresh."
# Nothing is broken when MASO simply is not part of this deployment, so this
# text states a fact and asks for no repair.
_MASO_NOT_CONFIGURED_MESSAGE = (
    "No MASO service is configured for this Hub, so there is no MASO quota to report."
)

# Cached full response: (expires_at_monotonic, response).
_QUOTA_CACHE: Optional[Tuple[float, QuotaResponse]] = None


def _iso_utc(epoch: float) -> Optional[str]:
    """Render a Unix timestamp as an ISO-8601 UTC string."""
    try:
        return (
            datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _valid_iso(value: Any) -> Optional[str]:
    """Pass an upstream ISO-8601 string through once it is known to parse."""
    if not isinstance(value, str) or not value:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _percent(value: Any) -> Optional[int]:
    """Clamp a provider percentage to a whole 0..100, or None when absent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        rounded = int(round(float(value)))
    except (OverflowError, ValueError):
        return None
    return max(0, min(100, rounded))


def _limit_state(value: Any) -> str:
    """Accept only the shared vocabulary; anything else is reported unknown."""
    return value if isinstance(value, str) and value in _LIMIT_STATES else "unknown"


def _worst_state(states: List[str]) -> str:
    """Pick the provider badge from its limits, most severe first."""
    if not states:
        return "unknown"
    return max(states, key=lambda state: _STATE_SEVERITY.get(state, 2))


# -- Codex -----------------------------------------------------------------


def _codex_home_dir() -> Path:
    """Codex CLI home directory (respects $CODEX_HOME if set)."""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def _codex_rollout_files() -> List[str]:
    """Return the newest rollout paths, newest-first by mtime.

    Ordering must come from mtime, not the ``YYYY/MM/DD`` directory or the
    timestamp in the filename: resuming an old session appends today's records
    to a file filed under its original date.

    Every rollout under both roots is stat'ed and only the newest
    ``_CODEX_MAX_CANDIDATES`` are kept, so the first entry is the globally
    newest file regardless of the order the filesystem enumerates in. Each
    root gets its own ``_CODEX_MAX_FILES_VISITED`` allowance so a crowded
    ``sessions/`` can never hide ``archived_sessions/``. That allowance is a
    runaway guard for a pathological tree, not a correctness budget: if it
    ever trips, the newest file can fall outside the visited set and the
    reading degrades visibly to an older ``as_of`` instead of failing.
    """
    home = _codex_home_dir()
    # Min-heap over (mtime_ns, path): the smallest is the first to be evicted.
    newest: List[Tuple[int, str]] = []
    for root in (home / "sessions", home / "archived_sessions"):
        if not root.is_dir():
            continue
        visited = 0
        for path in glob.iglob(str(root / "**" / "rollout-*.jsonl"), recursive=True):
            if visited >= _CODEX_MAX_FILES_VISITED:
                break
            visited += 1
            try:
                entry = (os.stat(path).st_mtime_ns, path)
            except OSError:
                continue
            if len(newest) < _CODEX_MAX_CANDIDATES:
                heapq.heappush(newest, entry)
            else:
                heapq.heappushpop(newest, entry)
    return [path for _mtime, path in sorted(newest, reverse=True)]


def _codex_tail(path: str) -> List[str]:
    """Read the last ``_CODEX_TAIL_BYTES`` of a rollout as lines."""
    with open(path, "rb") as source:
        size = os.fstat(source.fileno()).st_size
        if size > _CODEX_TAIL_BYTES:
            source.seek(size - _CODEX_TAIL_BYTES)
        raw = source.read(_CODEX_TAIL_BYTES)
    # A mid-line seek leaves a partial first line; it simply fails to parse.
    return raw.decode("utf-8", "ignore").split("\n")


def _codex_reading(path: str) -> Optional[Tuple[Optional[str], Dict[str, Any]]]:
    """Return ``(record timestamp, rate_limits)`` from the newest record.

    The quoted key is required: the bare word appears in ordinary conversation
    text, and only a ``token_count`` event carries a real reading.
    """
    for line in reversed(_codex_tail(path)):
        if '"rate_limits"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            return _valid_iso(record.get("timestamp")), rate_limits
    return None


def _codex_latest_reading() -> Optional[Tuple[Optional[str], Dict[str, Any]]]:
    """Find the newest usable reading, falling back across recent sessions."""
    reads = 0
    for path in _codex_rollout_files():
        if reads >= _CODEX_MAX_FILES_READ:
            break
        reads += 1
        try:
            reading = _codex_reading(path)
        except OSError:
            # A rotated or unreadable file is not a failure of the reading.
            continue
        if reading is not None:
            return reading
    return None


def _codex_scope(window_minutes: int) -> Tuple[str, str]:
    """Map a Codex rate-limit window to a stable scope id and display label."""
    if window_minutes == 300:
        return "5h", "5-hour limit"
    if window_minutes == 10080:
        return "weekly", "Weekly limit"
    return f"{window_minutes}m", f"{window_minutes}-minute limit"


def _codex_limit(window: Any, now: float) -> Optional[Tuple[QuotaLimit, bool]]:
    """Build one limit from a Codex window, flagging an elapsed reading.

    ``resets_at`` closes a rolling window: once it has passed, ``used_percent``
    describes a window that no longer exists (observed dropping from 50% to
    38% at a rollover), so the figure is withheld instead of shown as current.
    """
    if not isinstance(window, dict):
        return None
    minutes = window.get("window_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return None
    scope, label = _codex_scope(minutes)
    resets_raw = window.get("resets_at")
    resets_epoch = (
        float(resets_raw)
        if isinstance(resets_raw, (int, float)) and not isinstance(resets_raw, bool)
        else None
    )
    expired = resets_epoch is not None and now > resets_epoch
    used = None if expired else _percent(window.get("used_percent"))
    if used is None:
        state = "unknown"
    elif used >= CODEX_OVER_PERCENT:
        state = "over"
    elif used >= CODEX_NEAR_PERCENT:
        state = "near"
    else:
        state = "within"
    return (
        QuotaLimit(
            scope=scope,
            label=label,
            state=state,
            used_percent=used,
            remaining_percent=None if used is None else 100 - used,
            resets_at=_iso_utc(resets_epoch) if resets_epoch is not None else None,
        ),
        expired,
    )


def _codex_no_data() -> ProviderQuota:
    return ProviderQuota(
        provider="codex",
        available=False,
        state="no_data",
        message=_CODEX_NO_DATA_MESSAGE,
    )


def read_codex_quota() -> ProviderQuota:
    """Read the newest Codex rate-limit record written to this host."""
    try:
        reading = _codex_latest_reading()
    except (OSError, RuntimeError):
        logger.warning("codex quota: cannot scan rollout files", exc_info=True)
        return ProviderQuota(
            provider="codex",
            available=False,
            state="unavailable",
            message=_CODEX_UNREADABLE_MESSAGE,
        )
    if reading is None:
        return _codex_no_data()
    as_of, rate_limits = reading
    now = time.time()
    limits: List[QuotaLimit] = []
    stale = False
    for key in ("primary", "secondary"):
        built = _codex_limit(rate_limits.get(key), now)
        if built is None:
            continue
        limit, expired = built
        limits.append(limit)
        stale = stale or expired
    if not limits:
        # A record with no parseable window is no reading at all; keeping
        # available=True here would promise the panel rows it cannot draw.
        return _codex_no_data()
    credits = rate_limits.get("credits")
    balance = credits.get("balance") if isinstance(credits, dict) else None
    plan = rate_limits.get("plan_type")
    return ProviderQuota(
        provider="codex",
        available=True,
        state=_worst_state([limit.state for limit in limits]),
        message="",
        as_of=as_of,
        stale=stale,
        limits=limits,
        plan=plan if isinstance(plan, str) and plan else None,
        # Unit and precision are undocumented and the formatting drifts, so the
        # upstream string is forwarded verbatim rather than reinterpreted.
        credit_balance=balance if isinstance(balance, str) and balance else None,
    )


# -- MASO ------------------------------------------------------------------


def _maso_unavailable(message: str) -> ProviderQuota:
    return ProviderQuota(
        provider="maso",
        available=False,
        state="unavailable",
        message=message,
    )


def _maso_not_configured() -> ProviderQuota:
    return ProviderQuota(
        provider="maso",
        available=False,
        state="not_configured",
        message=_MASO_NOT_CONFIGURED_MESSAGE,
    )


def _maso_absent_from_deployment() -> bool:
    """Whether nothing on this host claims a MASO service at all.

    A deployment that never installs MASO must not be told to repair it; a
    permanent alert there would devalue the real ones. Explicit configuration
    and a local registry entry are both evidence that MASO belongs here, so a
    failed read is then a genuine fault. The configuration test mirrors the
    condition ``discover_maso_service`` uses to take its explicit target path,
    so this classification cannot drift from what discovery actually did.
    """
    if configured_maso_base_url({}) or os.environ.get("MASO_PORT"):
        return False
    try:
        return not local_maso_registry_ports()
    except MasoServiceUnavailable:
        # A registry too large to read is still evidence of a MASO install.
        return False


def _maso_limits(rows: Any) -> List[QuotaLimit]:
    """Map MASO's per-model rows, preferring its grant-adjusted figures."""
    limits: List[QuotaLimit] = []
    if not isinstance(rows, list):
        return limits
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("model")
        if not isinstance(model, str) or not model:
            continue
        # MASO has already applied grants and its own thresholds; an absent
        # effective figure means "no number", not "fall back to the raw one".
        used = _percent(row.get("effectiveUsedPercent", row.get("percentUsed")))
        limits.append(
            QuotaLimit(
                scope=model,
                label=model,
                state=_limit_state(row.get("effectiveState") or row.get("state")),
                used_percent=used,
                remaining_percent=None if used is None else 100 - used,
                resets_at=row.get("resetAt") if isinstance(row.get("resetAt"), str) else None,
            )
        )
    return limits


async def read_maso_quota(refresh: bool = False) -> ProviderQuota:
    """Ask the discovered local MASO service for its quota snapshot."""
    try:
        base_url, _models, _default = await discover_maso_service({})
    except MasoServiceUnavailable as exc:
        if _maso_absent_from_deployment():
            return _maso_not_configured()
        # Discovery messages name the configuration fix and carry no upstream
        # payload, so they are safe to show as-is.
        return _maso_unavailable(str(exc))
    except (ValueError, OSError):
        return _maso_unavailable(_MASO_UNAVAILABLE_MESSAGE)
    try:
        async with httpx.AsyncClient(
            base_url=base_url, trust_env=False, timeout=_MASO_TIMEOUT_S
        ) as client:
            response = await client.get(
                "/api/llm-quota", params={"refresh": "true"} if refresh else None
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, TimeoutError, ValueError):
        # Upstream status text may quote request detail; report Hub's own.
        logger.warning("maso quota: request failed", exc_info=True)
        return _maso_unavailable(_MASO_UNAVAILABLE_MESSAGE)
    if not isinstance(payload, dict) or payload.get("state") == "unavailable":
        return _maso_unavailable(_MASO_UNAVAILABLE_MESSAGE)
    enforcing = payload.get("enforcing")
    fetched_at = payload.get("fetchedAt")
    return ProviderQuota(
        provider="maso",
        available=True,
        state=_limit_state(payload.get("state")),
        message="",
        as_of=(
            _iso_utc(fetched_at)
            if isinstance(fetched_at, (int, float)) and not isinstance(fetched_at, bool)
            else None
        ),
        stale=False,
        limits=_maso_limits(payload.get("models")),
        enforcing=enforcing if isinstance(enforcing, bool) else None,
    )


# -- Combined reading ------------------------------------------------------


async def read_provider_quota(refresh: bool = False) -> QuotaResponse:
    """Read every provider, newest Codex record first, then MASO.

    One unreadable provider is reported as unavailable instead of failing the
    whole panel, so the broad guards below are deliberate. Results are cached
    briefly so a shared panel cannot turn each poll into a MASO discovery
    round; ``refresh`` bypasses both this cache and MASO's own.
    """
    global _QUOTA_CACHE

    now = time.monotonic()
    if not refresh and _QUOTA_CACHE is not None:
        expires_at, cached = _QUOTA_CACHE
        if now < expires_at:
            return cached
    try:
        codex = read_codex_quota()
    except Exception:
        logger.exception("codex quota: unexpected read failure")
        codex = ProviderQuota(
            provider="codex",
            available=False,
            state="unavailable",
            message=_CODEX_UNREADABLE_MESSAGE,
        )
    try:
        maso = await read_maso_quota(refresh=refresh)
    except Exception:
        logger.exception("maso quota: unexpected read failure")
        maso = _maso_unavailable(_MASO_UNAVAILABLE_MESSAGE)
    response = QuotaResponse(providers=[codex, maso])
    _QUOTA_CACHE = (time.monotonic() + _QUOTA_CACHE_TTL, response)
    return response
