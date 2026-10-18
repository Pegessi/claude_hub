"""Manager attribution responsibilities on the single terminal owner."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from ....models import AgentType, ExecutionTarget
from ..defaults import (
    _BACKFILL_MATCH_WINDOW_S,
    _CODEX_FENCE_SILENCE_S,
    _CODEX_NEW_PIN_TS_EARLY_S,
    _CODEX_NEW_PIN_TS_LATE_S,
    _CODEX_POLL_INTERVAL_S,
    _CODEX_POLL_MAX_ATTEMPTS,
    _SINGLE_TAB_DISCOVERY_DELAY_S,
)
from ..records import ScanEntry
from .host import ManagerHost

if TYPE_CHECKING:
    from ...ttyd_manager import TTYDProcess


logger = logging.getLogger("claude_hub.services.ttyd_manager")


class ManagerAttributionMixin(ManagerHost):
    def _backfill_agent_session_ids(self) -> None:
        """Conservatively pin ``agent_session_id`` for pre-feature claude tabs.

        Tabs that were already running before session-id pinning shipped have
        ``agent_session_id is None`` and cannot resume their real conversation
        after a reboot. While their tmux sessions are still alive (this runs at
        startup, before any relaunch), we correlate each such tab's tmux
        ``session_created`` time with the start time of conversations Claude
        logged for the tab's cwd. We pin an id ONLY when the match is
        unambiguous — cross-wiring a tab to the wrong conversation is worse than
        a fresh start, so anything uncertain is logged and skipped.

        Defensive throughout: any per-tab error is logged and never aborts
        startup. Each jsonl is read only up to its first timestamped line.
        """
        backfilled = False
        for process in list(self.processes.values()):
            try:
                if not (
                    process.from_persisted_state
                    and process.agent_type == AgentType.CLAUDE
                    and process.target == ExecutionTarget.LOCAL
                    and process.agent_session_id is None
                    and process.cwd
                ):
                    continue

                label = f"tab {process.tab_id} ({process.name})"

                session_created = self._runtime._tmux_session_created(process.tmux_session)
                if session_created is None:
                    logger.info(f"skipped session-id backfill for {label}: no live session")
                    continue

                project_dir = self._runtime._claude_project_dir_for_cwd(process.cwd)
                if not project_dir.is_dir():
                    logger.info(f"skipped session-id backfill for {label}: dir missing")
                    continue

                candidates: List[Tuple[float, str, str]] = []
                for jsonl_path in glob.glob(os.path.join(str(project_dir), "*.jsonl")):
                    start_epoch = self._runtime._jsonl_start_epoch(jsonl_path)
                    if start_epoch is None:
                        continue
                    session_id = os.path.basename(jsonl_path)[: -len(".jsonl")]
                    candidates.append((abs(start_epoch - session_created), session_id, jsonl_path))

                within_window = [c for c in candidates if c[0] <= _BACKFILL_MATCH_WINDOW_S]
                chosen = self._runtime._pick_backfill_session(session_created, candidates)
                if chosen is None:
                    if not within_window:
                        reason = "best delta too large"
                    else:
                        reason = f"{len(within_window)} candidates within window, ambiguous"
                    logger.info(f"skipped session-id backfill for {label}: {reason}")
                    continue

                process.agent_session_id = chosen
                backfilled = True
                logger.info(f"backfilled agent_session_id for {label} -> {chosen}")
            except Exception as e:  # never abort startup
                logger.warning(f"error during session-id backfill for tab {process.tab_id}: {e}")

        if backfilled:
            self._save_state()

    def _backfill_codex_session_ids(self) -> None:
        """Conservatively pin ``agent_session_id`` for codex tabs.

        Analogous to ``_backfill_agent_session_ids`` for Claude but operating on
        codex's rollout store under ``~/.codex/sessions/``. Runs at startup
        while tmux sessions are still alive so we can anchor on tmux
        ``session_created`` time. Pinning is conservative: cross-wiring a tab
        to the wrong conversation is worse than leaving it unpinned (in which
        case cold recovery starts fresh and attributes the new rollout).

        Also handles the upgrade case: tabs created before codex pinning
        shipped have a freshly-generated placeholder uuid at construction
        time; this backfill overwrites it with the codex-discovered real id
        when an unambiguous match is found.
        """
        backfilled = False
        scan: Optional[Dict[str, ScanEntry]] = None
        for process in list(self.processes.values()):
            try:
                if not (
                    process.from_persisted_state
                    and process.agent_type == AgentType.CODEX
                    and process.target == ExecutionTarget.LOCAL
                    and process.cwd
                ):
                    continue
                # Skip tabs that already have a real codex session id (exists
                # in session_index). Placeholder uuids generated at __init__
                # are not in the index and still need backfill.
                # One startup-local snapshot for both existence and candidate
                # checks. Never reuse it for cold-launch attribution, which
                # must observe changes made by newly launched providers.
                if scan is None:
                    scan = self._runtime._codex_scan_sessions()
                if process.agent_session_id and process.agent_session_id in scan:
                    continue

                label = f"tab {process.tab_id} ({process.name})"

                session_created = self._runtime._tmux_session_created(process.tmux_session)
                if session_created is None:
                    logger.info(f"skipped codex session-id backfill for {label}: no live session")
                    continue

                # Collect codex rollouts in this cwd and compute |start - session_created|.
                candidates: List[Tuple[float, str, str]] = []
                for start_epoch, sid, path in self._runtime._codex_candidates_for_cwd(
                    process.cwd, scan
                ):
                    candidates.append((abs(start_epoch - session_created), sid, path))

                within_window = [c for c in candidates if c[0] <= _BACKFILL_MATCH_WINDOW_S]
                chosen = self._runtime._pick_backfill_session(session_created, candidates)
                if chosen is None:
                    if not within_window:
                        reason = "best delta too large"
                    else:
                        reason = f"{len(within_window)} candidates within window, ambiguous"
                    logger.info(f"skipped codex session-id backfill for {label}: {reason}")
                    continue

                process.agent_session_id = chosen
                backfilled = True
                logger.info(f"backfilled codex agent_session_id for {label} -> {chosen}")
            except Exception as e:  # never abort startup
                logger.warning(
                    f"error during codex session-id backfill for tab {process.tab_id}: {e}"
                )

        if backfilled:
            self._save_state()

    async def _quarantine_codex_tab(self, tab: TTYDProcess, reason: str) -> None:
        """Fail-closed quarantine for a codex tab whose attribution failed.

        Kills the partially-started tmux session (so no stale codex process
        keeps writing to an unattributed rollout), clears ``agent_session_id``,
        resets all per-launch runtime fields, and sets
        ``resume_quarantined=True`` so the next launch starts fresh. The tab
        is NOT removed from the manager — it stays in processes so the UI
        still shows it; when the user next opens it (or the next restart),
        it will get a fresh codex session with a correctly-attributed sid.
        """
        logger.warning(
            "codex quarantine for tab %s (sid=%s): %s",
            tab.tab_id,
            tab.agent_session_id,
            reason,
        )
        # Kill the tmux session to stop a possibly-misattributed codex from
        # continuing to write to a rollout we no longer trust.
        stop_error: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                await tab.stop(kill_tmux=True)
                stop_error = None
                break
            except Exception as e:
                stop_error = e
                logger.error(
                    "quarantine: stop(kill_tmux=True) attempt %d for %s failed: %s",
                    attempt,
                    tab.tab_id,
                    e,
                )
        if stop_error is not None:
            # Do not erase ownership metadata while an unattributed writer may
            # still be alive.  Persist quarantine with the last known SID and
            # surface the cleanup failure to the caller/reviewer.
            tab.resume_quarantined = True
            tab._pending_quarantine_reason = f"{reason}; cleanup failed: {stop_error!r}"
            raise RuntimeError(
                f"failed to stop owned tmux/ttyd for quarantined tab {tab.tab_id}"
            ) from stop_error
        # Reset all per-launch state; clear agent_session_id so we don't carry
        # forward a now-untrustworthy sid. Phase 0 of the next start_all_tabs
        # will assign a fresh uuid4 for codex just like it does for legacy
        # cursor tabs.
        tab.agent_session_id = None
        tab._is_new_pin = None
        tab._pre_scan = None
        tab._launch_wall = None
        tab._launch_mono = None
        tab._pending_quarantine_reason = None
        tab.resume_quarantined = True
        tab.is_active = False

    async def _codex_poll_signal(self, tab: TTYDProcess) -> Dict[str, str]:
        """Fence-poll after launching a codex tab until activity is observed
        plus ``_CODEX_FENCE_SILENCE_S`` of silence.

        Returns the ``_diff_scans`` result against ``tab._pre_scan`` at the
        moment of the final post-fence snapshot, filtered to same-cwd
        ``new``/``appended`` entries (so cross-cwd activity from unrelated
        codex processes cannot leak into the per-tab signal). Polls at
        ``_CODEX_POLL_INTERVAL_S`` up to ``_CODEX_POLL_MAX_ATTEMPTS`` times
        (~6 s wall budget per tab).
        """
        assert tab._pre_scan is not None
        # ``tab._pre_scan`` is the immutable attribution baseline.  A second,
        # rolling snapshot is required for the silence fence: comparing every
        # poll to the immutable baseline makes the same already-observed append
        # look "new" forever and forces every launch to consume the full timeout.
        previous_post = tab._pre_scan
        saw_signal = False
        last_activity_at: Optional[float] = None
        try:
            target_cwd = os.path.realpath(tab.cwd) if tab.cwd else ""
        except OSError:
            target_cwd = tab.cwd or ""

        def _filter_same_cwd(post: Dict[str, ScanEntry], diff: Dict[str, str]) -> Dict[str, str]:
            out: Dict[str, str] = {}
            for s, ct in diff.items():
                if ct not in ("new", "appended"):
                    continue
                entry = post.get(s)
                if not entry or not entry.cwd:
                    continue
                if not target_cwd:
                    continue
                try:
                    if os.path.realpath(entry.cwd) != target_cwd:
                        continue
                except OSError:
                    continue
                out[s] = ct
            return out

        for _ in range(_CODEX_POLL_MAX_ATTEMPTS):
            await asyncio.sleep(_CODEX_POLL_INTERVAL_S)
            post = self._runtime._codex_scan_sessions()
            signal = _filter_same_cwd(post, self._runtime._diff_scans(tab._pre_scan, post))
            incremental = _filter_same_cwd(post, self._runtime._diff_scans(previous_post, post))
            now = time.monotonic()
            if signal:
                saw_signal = True
            if incremental:
                last_activity_at = now
            previous_post = post
            if (
                saw_signal
                and last_activity_at is not None
                and now - last_activity_at >= _CODEX_FENCE_SILENCE_S
            ):
                final_post = self._runtime._codex_scan_sessions()
                final_incremental = _filter_same_cwd(
                    final_post, self._runtime._diff_scans(previous_post, final_post)
                )
                if final_incremental:
                    # Activity raced with the settling poll; restart the silence
                    # window instead of accepting a moving target.
                    previous_post = final_post
                    last_activity_at = time.monotonic()
                    continue
                return _filter_same_cwd(
                    final_post, self._runtime._diff_scans(tab._pre_scan, final_post)
                )
        # Timeout: return last same-cwd diff we saw (caller treats empty as SignalZero)
        if saw_signal:
            # Re-scan once more to get a stable final view
            final_post = self._runtime._codex_scan_sessions()
            return _filter_same_cwd(
                final_post, self._runtime._diff_scans(tab._pre_scan, final_post)
            )
        return {}

    def _salvage_pending_codex_signal(self, tab: TTYDProcess) -> Optional[Tuple[str, str]]:
        """Resolve a failed signal before any sibling launch can begin.

        This runs while the global Codex launch lock is held and before the
        next same-cwd tab is started, so a candidate cannot belong to a later
        sibling.  Only exact append growth of the persisted target or exactly
        one new same-cwd rollout in this tab's timestamp window is salvageable.
        """
        assert tab._pre_scan is not None
        post = self._runtime._codex_scan_sessions()
        try:
            target_cwd = os.path.realpath(tab.cwd) if tab.cwd else ""
        except OSError:
            target_cwd = tab.cwd or ""

        def _same_cwd(entry: ScanEntry) -> bool:
            if not target_cwd or not entry.cwd:
                return False
            try:
                return bool(os.path.realpath(entry.cwd) == target_cwd)
            except OSError:
                return bool(entry.cwd == target_cwd)

        target_sid = tab.agent_session_id
        if target_sid:
            pre_target = tab._pre_scan.get(target_sid)
            post_target = post.get(target_sid)
            if (
                pre_target is not None
                and post_target is not None
                and _same_cwd(post_target)
                and post_target.size > pre_target.size
                and post_target.mtime_ns > pre_target.mtime_ns
            ):
                tab._is_new_pin = False
                tab.resume_quarantined = False
                tab._pending_quarantine_reason = None
                return "SALVAGE_RESUME", target_sid

        new_candidates: List[str] = []
        for sid, entry in post.items():
            if sid in tab._pre_scan or not _same_cwd(entry):
                continue
            if entry.ts is None or tab._launch_wall is None:
                continue
            delta = entry.ts - tab._launch_wall
            if -_CODEX_NEW_PIN_TS_EARLY_S <= delta <= _CODEX_NEW_PIN_TS_LATE_S:
                new_candidates.append(sid)
        if len(new_candidates) != 1:
            return None

        new_sid = new_candidates[0]
        tab.agent_session_id = new_sid
        tab._is_new_pin = True
        tab.resume_quarantined = False
        tab._pending_quarantine_reason = None
        return "SALVAGE_FRESH", new_sid

    async def _launch_one_cold_codex_locked(self, tab: TTYDProcess) -> Tuple[str, Optional[str]]:
        """Launch ONE cold codex tab under the global lock, attribute its
        session via Phase 1C signal logic, and pin ``tab.agent_session_id``.

        Returns ``(outcome, pinned_sid)`` where outcome is one of:
          RESUME_OK     — verified id resumed (file appended or new pin matched)
          FRESH_PIN     — no/quarantined/unverified id; started fresh; new sid pinned
          FALLBACK      — verified id FAILED to resume (new rollout with different sid);
                          pinned the new sid instead; quarantine cleared
          SALVAGE_*     — a signal race was resolved before any sibling launch
          PendingQ      — signal failure could not be proven; caller rolls back

        Launch exceptions propagate to the cwd-group rollback.  Signal failures
        get one immediate, launch-isolated salvage attempt; an unresolved signal
        returns PendingQ and the caller rolls back the whole cwd group before a
        sibling can start.
        """
        assert tab._launch_wall is None
        assert tab._pre_scan is None
        tab._pending_quarantine_reason = None  # type: ignore[attr-defined]
        target_sid = tab.agent_session_id
        is_verified = bool(
            not tab.resume_quarantined
            and target_sid
            and self._runtime._codex_id_exists(target_sid, tab.cwd)
        )
        tab._pre_scan = self._runtime._codex_scan_sessions()
        # ensure_tmux_session stamps _launch_wall/_launch_mono and spawns Codex.
        # The ttyd process itself is intentionally started only after all SID
        # attribution/reconciliation completes: ttyd does not participate in
        # rollout ownership, and serializing its configure/safety waits adds
        # ~1.5 s per tab without improving correctness.
        await tab.ensure_tmux_session()
        diff = await self._codex_poll_signal(tab)
        # Filter to same-cwd new/appended entries
        post = self._runtime._codex_scan_sessions()
        changed: Dict[str, str] = {}
        for s, ct in diff.items():
            if ct not in ("new", "appended"):
                continue
            entry = post.get(s)
            if not entry or not entry.cwd:
                continue
            if not tab.cwd:
                continue
            try:
                if os.path.realpath(entry.cwd) != os.path.realpath(tab.cwd):
                    continue
            except OSError:
                continue
            changed[s] = ct

        if not changed:
            reason = f"SignalZero (no same-cwd rollout activity within {_CODEX_POLL_MAX_ATTEMPTS * _CODEX_POLL_INTERVAL_S:.1f}s budget)"
            logger.warning("codex Phase 1C tab %s: %s", tab.tab_id, reason)
            tab._pending_quarantine_reason = reason  # type: ignore[attr-defined]
            return self._salvage_pending_codex_signal(tab) or ("PendingQ", None)

        if len(changed) != 1:
            reason = (
                f"SignalAmbiguous ({len(changed)} same-cwd changed sids: {sorted(changed.keys())})"
            )
            logger.warning("codex Phase 1C tab %s: %s", tab.tab_id, reason)
            tab._pending_quarantine_reason = reason  # type: ignore[attr-defined]
            return self._salvage_pending_codex_signal(tab) or ("PendingQ", None)

        ((new_sid, ct),) = changed.items()
        is_appended = ct == "appended"
        entry = post[new_sid]

        if is_appended:
            # Successful resume appended to the target file.
            if is_verified and new_sid == target_sid:
                tab._is_new_pin = False
                tab.resume_quarantined = False
                logger.info(
                    "codex Phase 1C tab %s: RESUME_OK append-resume to %s",
                    tab.tab_id,
                    new_sid,
                )
                return "RESUME_OK", new_sid
            # Appending to a non-target sid: unexpected, defer to Phase R.
            reason = f"SignalBadDiscriminator: append to {new_sid} but target was {target_sid}"
            logger.warning("codex Phase 1C tab %s: %s", tab.tab_id, reason)
            tab._pending_quarantine_reason = reason  # type: ignore[attr-defined]
            return self._salvage_pending_codex_signal(tab) or ("PendingQ", None)

        # New file: codex created a fresh rollout.
        if entry.ts is not None:
            delta = entry.ts - (tab._launch_wall or time.time())
            if delta < -_CODEX_NEW_PIN_TS_EARLY_S or delta > _CODEX_NEW_PIN_TS_LATE_S:
                reason = f"SignalBadDiscriminator: new rollout {new_sid} ts delta {delta:.1f}s outside [-{_CODEX_NEW_PIN_TS_EARLY_S:.0f}s, +{_CODEX_NEW_PIN_TS_LATE_S:.0f}s] window"
                logger.warning("codex Phase 1C tab %s: %s", tab.tab_id, reason)
                tab._pending_quarantine_reason = reason  # type: ignore[attr-defined]
                return self._salvage_pending_codex_signal(tab) or ("PendingQ", None)
        if is_verified and new_sid == target_sid:
            # Verified resume that also created new file — shouldn't happen for a
            # resume, but if sids match, trust it.
            tab._is_new_pin = False
            tab.resume_quarantined = False
            logger.info(
                "codex Phase 1C tab %s: RESUME_OK (new file but sid matches %s)",
                tab.tab_id,
                new_sid,
            )
            return "RESUME_OK", new_sid
        # Either: verified resume failed and codex fell through to fresh (FALLBACK),
        # or unverified/quarantined tab started fresh (FRESH_PIN). Both pin new_sid.
        tab.agent_session_id = new_sid
        tab._is_new_pin = True
        tab.resume_quarantined = False
        outcome = "FALLBACK" if is_verified else "FRESH_PIN"
        logger.info(
            "codex Phase 1C tab %s: %s -> %s",
            tab.tab_id,
            outcome,
            new_sid,
        )
        return outcome, new_sid

    def _schedule_codex_discovery(
        self, process: TTYDProcess, delay: float = _SINGLE_TAB_DISCOVERY_DELAY_S
    ) -> None:
        """Fire-and-forget: discover and pin codex session id after ``delay``.

        Used when a SINGLE codex tab is launched outside the bulk
        ``start_all_tabs`` path (create_tab, ensure_tab_tmux_session). Single
        launches never run under GLOBAL_CODEX_LAUNCH_LOCK and skip Phase-R
        because there is only one codex writing to disk at a time, so the
        legacy timestamp-window discovery is sufficient.
        """
        if process.agent_type != AgentType.CODEX:
            return
        if process.target != ExecutionTarget.LOCAL:
            return
        if not process.cwd:
            return

        async def _discover() -> None:
            await asyncio.sleep(delay)
            try:
                launch_epoch = (
                    process._launch_wall
                    if process._launch_wall is not None
                    else time.time() - delay
                )
                if process.agent_session_id and self._runtime._codex_id_exists(
                    process.agent_session_id, process.cwd
                ):
                    return
                discovered = process._discover_codex_session_id(launch_epoch)
                if discovered and discovered != process.agent_session_id:
                    process.agent_session_id = discovered
                    process.resume_quarantined = False
                    self._save_state()
            except Exception as e:
                logger.warning(f"error during single-tab codex discovery for {process.tab_id}: {e}")

        asyncio.create_task(_discover())
