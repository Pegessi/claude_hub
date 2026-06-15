"""In-memory store for Feishu card-action results (Scenario A bridge).

Scenario A: an external agent invokes ``claude-hub feishu send-card --wait``.
The CLI generates an opaque token, registers it here, pushes an interactive
card to a human over Feishu, and then long-polls for the human's decision. When
the human clicks a button (or submits a form), the long-connection bot receives
a ``card.action.trigger`` callback and POSTs the decision back, keyed by the
same token. The blocked CLI poll then returns the result.

The data is intentionally transient: a correlation token lives only for the
duration of one CLI invocation's wait. An in-memory dict with a TTL is the
right scope -- nothing here needs to survive a backend restart, and persisting
half-answered prompts would be a liability, not a feature. Entries are pruned
opportunistically on every access and explicitly via :meth:`prune`.

Thread-safety: the long-connection bot posts results from a worker thread while
FastAPI serves polls from the event loop's threadpool, so all mutating access
is guarded by a single lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

# How long a registered-but-unanswered token is retained before it is treated as
# expired and eligible for pruning. Generous enough to cover a human stepping
# away mid-decision, bounded enough that abandoned prompts cannot accumulate.
DEFAULT_TTL = timedelta(hours=1)

# Status values returned to a polling CLI.
STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"
STATUS_UNKNOWN = "unknown"
STATUS_EXPIRED = "expired"


@dataclass
class _Entry:
    """A single registered card-action correlation slot."""

    token: str
    created_at: datetime
    chat_id: Optional[str] = None
    kind: Optional[str] = None
    resolved_at: Optional[datetime] = None
    action: Optional[str] = None
    form: Dict[str, Any] = field(default_factory=dict)
    operator_id: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None


class CardResultStore:
    """Thread-safe, TTL-bounded store of pending/resolved card actions."""

    def __init__(self, ttl: timedelta = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._entries: Dict[str, _Entry] = {}

    def register(
        self,
        token: str,
        *,
        chat_id: Optional[str] = None,
        kind: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Register a freshly generated token as pending.

        Re-registering an existing token is a no-op so a CLI retry cannot clobber
        a decision that already landed.
        """
        now = now or datetime.now()
        with self._lock:
            self._prune_locked(now)
            if token in self._entries:
                return
            self._entries[token] = _Entry(token=token, created_at=now, chat_id=chat_id, kind=kind)

    def submit(
        self,
        token: str,
        *,
        action: Optional[str],
        form: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Record a human decision for ``token``.

        Returns ``True`` if the decision was stored, ``False`` if the token is
        unknown/expired or was already resolved (first write wins -- a double
        click cannot overwrite the recorded decision).
        """
        now = now or datetime.now()
        with self._lock:
            self._prune_locked(now)
            entry = self._entries.get(token)
            if entry is None or entry.resolved:
                return False
            entry.action = action
            entry.form = dict(form or {})
            entry.operator_id = operator_id
            entry.resolved_at = now
            return True

    def get(self, token: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Return the current status payload for ``token``.

        Status is one of ``pending`` / ``resolved`` / ``unknown``. Expired
        unresolved tokens are pruned and reported as ``unknown``.
        """
        now = now or datetime.now()
        with self._lock:
            self._prune_locked(now)
            entry = self._entries.get(token)
            if entry is None:
                return {"token": token, "status": STATUS_UNKNOWN}
            if entry.resolved:
                return {
                    "token": token,
                    "status": STATUS_RESOLVED,
                    "action": entry.action,
                    "form": entry.form,
                    "operator_id": entry.operator_id,
                    "chat_id": entry.chat_id,
                    "kind": entry.kind,
                    "resolved_at": entry.resolved_at.isoformat() if entry.resolved_at else None,
                }
            return {
                "token": token,
                "status": STATUS_PENDING,
                "chat_id": entry.chat_id,
                "kind": entry.kind,
                "created_at": entry.created_at.isoformat(),
            }

    def discard(self, token: str) -> None:
        """Drop a token's slot (e.g. after a CLI wait times out or completes)."""
        with self._lock:
            self._entries.pop(token, None)

    def prune(self, *, now: Optional[datetime] = None) -> int:
        """Remove expired entries; return the count removed."""
        now = now or datetime.now()
        with self._lock:
            return self._prune_locked(now)

    def _prune_locked(self, now: datetime) -> int:
        """Drop entries past TTL. Caller must hold the lock.

        Resolved entries are kept until TTL past their resolution so a slightly
        late CLI poll can still read the decision; unresolved entries expire
        relative to creation.
        """
        cutoff = now - self._ttl
        expired = [
            token
            for token, entry in self._entries.items()
            if (entry.resolved_at or entry.created_at) < cutoff
        ]
        for token in expired:
            del self._entries[token]
        return len(expired)


# Module-level singleton used by the API router and (via it) the bot callback.
card_result_store = CardResultStore()
