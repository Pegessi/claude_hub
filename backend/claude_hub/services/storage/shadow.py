"""Shadow storage wrapper — primary-first writes with opt-in secondary dup.

The :class:`ShadowStorageBackend` wraps two :class:`~claude_hub.services.storage.StorageBackend`
instances: a *primary* (the source of truth on reads, and the first writer on
saves) and a *secondary* (best-effort duplicate for drift detection / future
cutover).

Critical safety properties:

* **Reads always come from the primary.** The secondary is write-only in this
  phase, so a misconfigured or corrupt secondary never affects serving state.
* **Primary writes are committed before the secondary is touched.** If the
  primary raises, the secondary is never called and the exception propagates.
* **Secondary failures never fail the save.** Any exception from the secondary
  is routed to ``on_error`` (a callback, typically ``logger.warning``) and
  swallowed so primary durability is not compromised.
* **Drift is reported, not enforced.** After a successful secondary save, the
  wrapper reloads both backends and compares fingerprints; any drift is passed
  to ``on_error`` as a :class:`ShadowDrift` warning but does not raise.
* **Opt-in only.** This module is never imported by the running workspace
  manager unless an explicit operator flag enables shadow writes. The default
  server path never constructs a :class:`ShadowStorageBackend`.

A helper :func:`assert_path_outside_root` enforces AC6 (live-root guard): the
secondary storage location (typically a SQLite file) must not live inside any
state root that the primary could be writing to, preventing accidental co-
location of shadow artifacts with live JSON.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import StorageBackend, StorageSnapshot
from .verify import _diff, _fingerprint

logger = logging.getLogger(__name__)


@dataclass
class ShadowDrift:
    """Structured drift warning between primary and secondary after a save."""

    primary_missing_from_secondary: dict[str, List[str]] = field(default_factory=dict)
    secondary_extra: dict[str, List[str]] = field(default_factory=dict)
    changed: dict[str, List[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not (self.primary_missing_from_secondary or self.secondary_extra or self.changed)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "primary_missing_from_secondary": self.primary_missing_from_secondary,
            "secondary_extra": self.secondary_extra,
            "changed": self.changed,
        }

    def describe(self) -> str:
        if self.ok:
            return "shadow: no drift"
        parts: list[str] = []
        for kind, ids in self.primary_missing_from_secondary.items():
            if ids:
                parts.append(
                    f"{kind} missing from secondary: {len(ids)} ({', '.join(ids[:3])}{'…' if len(ids) > 3 else ''})"
                )
        for kind, ids in self.secondary_extra.items():
            if ids:
                parts.append(
                    f"{kind} extra in secondary: {len(ids)} ({', '.join(ids[:3])}{'…' if len(ids) > 3 else ''})"
                )
        for kind, ids in self.changed.items():
            if ids:
                parts.append(
                    f"{kind} changed between backends: {len(ids)} ({', '.join(ids[:3])}{'…' if len(ids) > 3 else ''})"
                )
        return "shadow drift: " + "; ".join(parts)


class ShadowError(RuntimeError):
    """Raised for configuration errors (e.g. secondary path inside live root)."""


def _is_under(path: Path, root: Path) -> bool:
    """Return True if ``path`` is (or would be created) inside ``root``.

    Uses :meth:`Path.resolve` to collapse symlinks/``..``; falls back to
    lexical comparison if resolution fails (e.g. path doesn't exist yet).
    """
    try:
        p = path.resolve()
        r = root.resolve()
    except OSError:
        p = Path(path).expanduser().absolute()
        r = Path(root).expanduser().absolute()
    try:
        p.relative_to(r)
        return True
    except ValueError:
        return False


def assert_path_outside_root(
    candidate: Path,
    *forbidden_roots: Path,
    label: str = "shadow path",
) -> None:
    """Raise :class:`ShadowError` if ``candidate`` lives under any forbidden root.

    Used at shadow-enable time to guarantee AC6: shadow artifacts (e.g. a SQLite
    file, or the directory that will hold it) never live inside a live state
    root where an operator mistake could cause them to be treated as authoritative.
    """
    candidate = Path(candidate)
    # Check both the candidate itself and its parent (so a sqlite path like
    # ``root/state.sqlite3`` under a forbidden root is caught even if the file
    # does not exist yet).
    targets = [candidate]
    if candidate.parent != candidate:
        targets.append(candidate.parent)
    for root in forbidden_roots:
        root = Path(root)
        for t in targets:
            if _is_under(t, root):
                raise ShadowError(
                    f"{label} {candidate} must not live under state root {root}; "
                    "refusing to enable shadow writes into a live state directory."
                )


class ShadowStorageBackend:
    """Primary-first storage backend with a best-effort secondary duplicate.

    Implements the :class:`~claude_hub.services.storage.StorageBackend` protocol
    structurally (``@runtime_checkable``) so it can be passed anywhere a
    ``StorageBackend`` is expected.
    """

    def __init__(
        self,
        primary: StorageBackend,
        secondary: StorageBackend,
        *,
        on_error: Optional[Callable[[Exception], None]] = None,
        compare_after_save: bool = True,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.on_error = on_error or (lambda exc: logger.warning("shadow secondary error: %s", exc))
        self.compare_after_save = compare_after_save
        self.last_drift: Optional[ShadowDrift] = None

    def load(self) -> StorageSnapshot:
        """Load from the primary only. The secondary is write-only."""
        return self.primary.load()

    def save(self, snapshot: StorageSnapshot) -> None:
        """Save to primary first; duplicate to secondary on best-effort basis.

        * If primary.save() raises, the exception propagates unchanged and the
          secondary is never touched.
        * If secondary.save() raises, the exception is routed to ``on_error``
          and swallowed — primary durability is preserved.
        * If ``compare_after_save`` is True, reloads both backends and diffs
          fingerprints; drift is passed to ``on_error`` as a :class:`ShadowDrift`
          wrapped in a :class:`ShadowDriftWarning` (or plain Exception) but
          does not raise.
        """
        # Primary is the source of truth: commit first and alone.
        self.primary.save(snapshot)

        try:
            self.secondary.save(snapshot)
        except Exception as exc:  # noqa: BLE001 - secondary is best-effort, never raise
            self.on_error(exc)
            return

        if not self.compare_after_save:
            self.last_drift = None
            return

        try:
            drift = self._compare()
        except Exception as exc:  # noqa: BLE001 - drift compare must not break save
            self.on_error(exc)
            return
        self.last_drift = drift
        if not drift.ok:
            self.on_error(ShadowDriftWarning(drift))

    def _compare(self) -> ShadowDrift:
        primary_snap = self.primary.load()
        secondary_snap = self.secondary.load()
        p_fp = _fingerprint(primary_snap)
        s_fp = _fingerprint(secondary_snap)
        drift = ShadowDrift()
        for kind in ("workspaces", "tasks", "sessions", "reports"):
            d = _diff(kind, p_fp, s_fp)
            if d.missing:
                drift.primary_missing_from_secondary[kind] = d.missing
            if d.extra:
                drift.secondary_extra[kind] = d.extra
            if d.changed:
                drift.changed[kind] = d.changed
        return drift


class ShadowDriftWarning(RuntimeWarning):
    """Raised (and routed to on_error) when primary and secondary diverge."""

    def __init__(self, drift: ShadowDrift) -> None:
        super().__init__(drift.describe())
        self.drift = drift
