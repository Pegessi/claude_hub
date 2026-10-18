"""Manager startup responsibilities on the single terminal owner."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
import uuid
from typing import TYPE_CHECKING, Dict, List, Optional

from ....models import AgentType, ExecutionTarget
from ..defaults import (
    _CODEX_NEW_PIN_TS_EARLY_S,
    _CODEX_NEW_PIN_TS_LATE_S,
    _MANAGED_TMUX_SESSION_RE,
    _ORPHAN_TMUX_PRUNE_GRACE_SECONDS,
)
from ..records import ScanEntry
from .host import ManagerHost

if TYPE_CHECKING:
    from ...ttyd_manager import TTYDProcess


logger = logging.getLogger("claude_hub.services.ttyd_manager")


class ManagerStartupMixin(ManagerHost):
    async def start_all_tabs(self) -> None:
        """Start all saved tabs on startup. Five-phase lifecycle:

        Phase 0 — Snapshot and backfill. Backfill codex sids against live
        tmux sessions (where unambiguous) and assign new uuid4 sids to any
        cursor tabs missing them (Cursor is a constructive pin; V0 verified
        ``agent --resume <uuid>`` creates the store immediately).

        Phase 1 (hot) — If at least one persisted tab still has its exact tmux
        session, treat startup as a backend-only restart and reattach only the
        tabs whose tmux sessions survived. Persisted tabs without a surviving
        session stay stopped and can be resumed explicitly when opened. This
        prevents a source reload from cold-launching every historical tab.

        Phase 1 (cold) — If none of the persisted tabs has a surviving tmux
        session, treat startup as a machine/runtime restart and recover all
        saved tabs using the existing pinned-session recovery rules.

        Phase 1S — Serialize codex cold launches under GLOBAL_CODEX_LAUNCH_LOCK.
        Group tabs by cwd; within each cwd run VerifiedCodex (verified sid,
        not quarantined) BEFORE FreshCodex (new / unverified / quarantined) —
        this ordering guarantees that a resumed-verified tab appending to an
        existing rollout is observed before any fresh codex in that cwd
        starts, preventing R8 extra-sid misattribution. Across cwds the
        ordering also prefers fewer-cwd keys first (cwd-first sort) for
        determinism. Each tab's launch uses the per-tab snapshot+signal
        logic (_launch_one_cold_codex_locked) with atomic whole-cwd rollback:
        if any tab in a cwd group throws, stop(kill_tmux=True) every tab in
        that cwd and mark them quarantine=True.

        Phase R — After all successfully-attributed codex tabs are launched,
        re-scan and enforce R1-R8 bijection:
          R1 every expected pinned sid exists in post scan
          R2 every entry.cwd realpath matches the tab's cwd realpath
          R3 new-pin sids fall in ts window; append-resume sids grew
             (size+mtime_ns vs _pre_scan)
          R4 no empty entries
          R5 no duplicate sid assignments across tabs
          R6 every actual new-pin sid in this batch is assigned to exactly one tab
          R7 every append-resume sid was present in that tab's exact _pre_scan
          R8 extras: any new same-cwd sid relative to the immutable global
             pre-launch scan is rejected
        Any R6-R8 failure quarantines the whole live cwd batch rather than
        risking cross-wiring. Signal failures are salvaged, if provable, while
        the lock is still held and before the next sibling launches; unresolved
        ownership rolls back the cwd immediately.

        Phase F — Persist final state to tabs.json so the next restart can
        resume the correctly-pinned sids.
        """
        self._require_valid_state()
        runtime_processes = [
            process for process in self.processes.values() if not process.is_managed_chat
        ]
        for process in self.processes.values():
            if process.is_managed_chat:
                process.process = None
                process.is_active = False
        if not runtime_processes:
            # A headless managed Chat restore must not initialize tmux merely
            # because a navigation tab record exists.
            self._save_state()
            return

        logger.info("Ensuring tmux server is running...")
        self._runtime._ensure_tmux_server()

        # Repair raw tmux sessions that survived an older failed/cancelled tab
        # lifecycle and no longer have a durable tabs.json owner.  This runs
        # before the API accepts requests and after the backend instance lock
        # is acquired, so the grace window only needs to cover historical
        # create crashes rather than a competing live backend.
        await self._prune_orphan_tmux_sessions()

        # Phase 0: backfill + assign sids for cursor legacy tabs.
        self._backfill_agent_session_ids()
        self._backfill_codex_session_ids()
        for p in self.processes.values():
            if (
                p.agent_type == AgentType.CURSOR
                and p.target == ExecutionTarget.LOCAL
                and not p.agent_session_id
            ):
                p.agent_session_id = str(uuid.uuid4())
                p.resume_quarantined = False

        try:
            listed = subprocess.run(
                self._runtime.tmux_command("ls"),
                capture_output=True,
                text=True,
            )
            result = listed.stdout or listed.stderr
            logger.info("Existing tmux sessions before starting tabs:\n%s", result)
        except Exception as e:
            logger.debug("Could not list tmux sessions: %s", e)

        processes = runtime_processes
        logger.info("Starting %d saved tabs", len(processes))

        # Partition into hot (session exists) / cold-codex / cold-non-codex.
        # A mixed snapshot means the tmux server survived this backend restart.
        # Missing sessions in that case are historical/stopped tabs, not proof
        # that every one of them should be relaunched. They remain available in
        # the UI and ``ensure_tab_running`` resumes one on explicit access.
        async def _session_exists(p: TTYDProcess) -> bool:
            return await self._runtime._tmux_session_exists_async(p.tmux_session)

        exists = await asyncio.gather(*(_session_exists(p) for p in processes))
        hot_restart = any(exists)
        hot: List[TTYDProcess] = []
        cold_non_codex: List[TTYDProcess] = []
        cold_codex: List[TTYDProcess] = []
        for p, ex in zip(processes, exists):
            if p.archived:
                # Archived tabs stay stopped on startup; restored on demand
                # via unarchive / deep link.
                p.is_active = False
                continue
            if ex:
                hot.append(p)
            elif hot_restart:
                p.is_active = False
            elif p.agent_type == AgentType.CODEX and p.target == ExecutionTarget.LOCAL:
                cold_codex.append(p)
            else:
                cold_non_codex.append(p)

        if hot_restart:
            skipped = len(processes) - len(hot)
            logger.info(
                "Hot backend restart detected: reattaching %d live tabs and "
                "leaving %d tabs stopped until explicitly opened",
                len(hot),
                skipped,
            )
        else:
            logger.info(
                "Cold runtime restart detected: recovering all %d saved tabs",
                len(processes),
            )

        # Phase 1: launch hot + cold non-codex in parallel.
        async def _start_one(process: TTYDProcess) -> None:
            try:
                await process.start()
                logger.info(
                    "Tab %s started (tmux session %s %s)",
                    process.tab_id,
                    process.tmux_session,
                    "reattached" if await _session_exists(process) else "created",
                )
            except Exception as e:
                logger.error("Failed to start tab %s: %s", process.tab_id, e)

        async def _launch_cold_non_codex(p: TTYDProcess) -> None:
            # For claude/cursor/terminal cold launches we stamp _launch_wall via
            # ensure_tmux_session so the clock is captured consistently.
            # start() uses tmux new-session -A; calling ensure first ensures we
            # create the session and stamp clocks before ttyd binds the port.
            if not await self._runtime._tmux_session_exists_async(p.tmux_session):
                try:
                    await p.ensure_tmux_session()
                except Exception as e:
                    logger.warning(
                        "ensure_tmux_session for non-codex tab %s failed: %s", p.tab_id, e
                    )
            await _start_one(p)

        await asyncio.gather(
            *(_start_one(p) for p in hot),
            *(_launch_cold_non_codex(p) for p in cold_non_codex),
        )

        # Phase 1S: cold codex, per-cwd serial under global lock.
        # Group cold codex by cwd and sort: (is_verified, tab_id) within each cwd,
        # sorted cwd keys across cwds (deterministic). Verified-before-Fresh
        # guarantees resumed-verified tabs append before any fresh codex starts
        # in the same cwd, preventing R8 extra-sid misattribution.
        from collections import defaultdict

        by_cwd: Dict[str, List[TTYDProcess]] = defaultdict(list)
        for p in cold_codex:
            key = os.path.realpath(p.cwd) if p.cwd else ""
            by_cwd[key].append(p)
        for cwd_key in list(by_cwd.keys()):
            tabs = by_cwd[cwd_key]
            # Verified-before-Fresh: tabs with a verified, non-quarantined sid
            # go first so that a successful append-resume is observed before
            # any fresh codex in this cwd starts writing.
            tabs.sort(
                key=lambda t: (
                    (
                        0
                        if (
                            not t.resume_quarantined
                            and t.agent_session_id
                            and self._runtime._codex_id_exists(t.agent_session_id, t.cwd)
                        )
                        else 1
                    ),
                    t.tab_id,
                )
            )

        async with self._runtime.GLOBAL_CODEX_LAUNCH_LOCK:
            pre_global_scan = self._runtime._codex_scan_sessions() if cold_codex else {}
            launched_codex: List[TTYDProcess] = []
            for cwd_key in sorted(by_cwd.keys()):
                tabs = by_cwd[cwd_key]
                started_this_cwd: List[TTYDProcess] = []
                try:
                    for tab in tabs:
                        outcome, pinned = await self._launch_one_cold_codex_locked(tab)
                        started_this_cwd.append(tab)
                        if outcome == "PendingQ":
                            raise RuntimeError(
                                f"unresolved Codex ownership signal for tab {tab.tab_id}: "
                                f"{tab._pending_quarantine_reason}"
                            )
                        launched_codex.append(tab)
                except Exception as e:
                    logger.error(
                        "Cold codex launch cwd-group rollback for cwd %s: %s",
                        cwd_key,
                        e,
                    )
                    # Atomic rollback: _quarantine_codex_tab kills tmux, clears SID,
                    # resets runtime fields, and sets resume_quarantined=True.
                    rollback_failures: List[str] = []
                    for t in tabs:
                        try:
                            await self._quarantine_codex_tab(
                                t, f"cwd-group exception rollback: {e!r}"
                            )
                        except Exception as qe:
                            logger.error("rollback quarantine for %s failed: %s", t.tab_id, qe)
                            rollback_failures.append(f"{t.tab_id}: {qe!r}")
                    self._save_state()
                    # Remove all tabs in this cwd from launched_codex; Phase R skips them.
                    launched_codex = [x for x in launched_codex if x not in tabs]
                    if rollback_failures:
                        raise RuntimeError(
                            "Codex cwd rollback could not prove process teardown: "
                            + "; ".join(rollback_failures)
                        ) from e
                    continue

            # Phase R must stay inside the same global lock as launch. Releasing
            # the lock first would let another cold-recovery batch create rollout
            # activity between our final scan and ownership decision.
            await self._reconcile_codex_phase_r(launched_codex, pre_global_scan)

        # Attribution is complete and the global lock has excluded competing
        # cold Codex recovery.  Start the independent ttyd frontends in
        # parallel; quarantined tabs have no owned tmux and remain stopped.
        await asyncio.gather(
            *(_start_one(tab) for tab in launched_codex if not tab.resume_quarantined)
        )

        # Final persist.
        self._save_state()

    async def _prune_orphan_tmux_sessions(
        self,
        *,
        grace_seconds: float = _ORPHAN_TMUX_PRUNE_GRACE_SECONDS,
    ) -> list[str]:
        """Kill old managed-prefix tmux sessions absent from ``tabs.json``.

        Manual/non-Hub tmux sessions and recently-created managed sessions are
        preserved.  Failed kills remain live and are reported for a later
        restart instead of being counted as successfully pruned.
        """
        owned = {process.tmux_session for process in self.processes.values()}
        now = time.time()
        pruned: list[str] = []
        sessions = await self._runtime._tmux_list_session_created()
        for session_name, created_at in sorted(sessions.items()):
            if not _MANAGED_TMUX_SESSION_RE.fullmatch(session_name):
                continue
            if session_name in owned:
                continue
            if now - created_at < grace_seconds:
                continue
            logger.warning(
                "Pruning orphan tmux session with no tabs.json owner session=%s age_seconds=%.1f",
                session_name,
                now - created_at,
            )
            try:
                await self._runtime._tmux_kill_session(session_name)
            except Exception:
                logger.exception("Failed to prune orphan tmux session %s", session_name)
                continue
            pruned.append(session_name)
        return pruned

    async def _reconcile_codex_phase_r(
        self, launched: List[TTYDProcess], pre_global_scan: Dict[str, ScanEntry]
    ) -> None:
        """Phase R: enforce the final R1-R8 ownership bijection.

        Any failure calls ``_quarantine_codex_tab`` (which kills tmux, clears
        the SID only after teardown is proven, resets runtime fields, and sets
        ``resume_quarantined=True``) rather than risking cross-wiring.
        """
        if not launched:
            return
        post = self._runtime._codex_scan_sessions()
        problems: List[str] = []
        quarantine_failures: List[str] = []
        cwd_to_tabs: Dict[str, List[TTYDProcess]] = {}
        for t in launched:
            key = os.path.realpath(t.cwd) if t.cwd else ""
            cwd_to_tabs.setdefault(key, []).append(t)

        def _same_cwd(entry: ScanEntry, cwd_key: str) -> bool:
            if not entry.cwd or not cwd_key:
                return False
            try:
                return bool(os.path.realpath(entry.cwd) == cwd_key)
            except OSError:
                return bool(entry.cwd == cwd_key)

        # ---- R1-R5 per-tab bijection ----
        quarantine_reasons: Dict[str, List[str]] = {}

        def _queue_quarantine(tab: TTYDProcess, reason: str) -> None:
            quarantine_reasons.setdefault(tab.tab_id, []).append(reason)

        sid_claims: Dict[str, List[TTYDProcess]] = {}
        for t in launched:
            if t._pending_quarantine_reason is not None:
                _queue_quarantine(t, f"R-fail signal/launch: {t._pending_quarantine_reason}")
            elif not t.agent_session_id:
                _queue_quarantine(t, f"tab {t.tab_id} R4 empty agent_session_id")
            else:
                sid_claims.setdefault(t.agent_session_id, []).append(t)

        # A duplicate invalidates every claimant, not only whichever tab happens
        # to be visited second.
        for claimed_sid, claimants in sid_claims.items():
            if len(claimants) > 1:
                labels = sorted(t.tab_id for t in claimants)
                for t in claimants:
                    _queue_quarantine(t, f"tab {t.tab_id} R5 duplicate sid {claimed_sid}: {labels}")

        for t in launched:
            label = f"tab {t.tab_id}"
            if quarantine_reasons.get(t.tab_id):
                continue
            sid: Optional[str] = t.agent_session_id
            assert sid is not None
            post_entry: Optional[ScanEntry] = post.get(sid)
            # R1 exists
            if post_entry is None:
                _queue_quarantine(t, f"{label} R1 sid {sid} missing from post-scan")
                continue
            # R2 cwd realpath match
            if t.cwd and not post_entry.cwd:
                _queue_quarantine(t, f"{label} R2 rollout cwd missing")
                continue
            if t.cwd and post_entry.cwd:
                try:
                    if os.path.realpath(post_entry.cwd) != os.path.realpath(t.cwd):
                        _queue_quarantine(
                            t, f"{label} R2 cwd mismatch: {post_entry.cwd} vs {t.cwd}"
                        )
                except OSError:
                    if post_entry.cwd != t.cwd:
                        _queue_quarantine(
                            t, f"{label} R2 cwd mismatch: {post_entry.cwd} vs {t.cwd}"
                        )
            if quarantine_reasons.get(t.tab_id):
                continue
            # R3 check
            pre_entry = t._pre_scan.get(sid) if t._pre_scan is not None else None
            r3_fail: Optional[str] = None
            if t._is_new_pin is True:
                if post_entry.ts is None or t._launch_wall is None:
                    r3_fail = f"{label} R3-new missing ts"
                else:
                    delta = post_entry.ts - t._launch_wall
                    if delta < -_CODEX_NEW_PIN_TS_EARLY_S or delta > _CODEX_NEW_PIN_TS_LATE_S:
                        r3_fail = f"{label} R3-new ts delta {delta:.1f}s outside window"
            elif t._is_new_pin is False:
                # append-resume: must have grown size+mtime vs pre
                if pre_entry is None:
                    r3_fail = f"{label} R3-append no pre_entry for resumed sid {sid}"
                else:
                    size_grew = post_entry.size > pre_entry.size
                    mtime_grew = post_entry.mtime_ns > pre_entry.mtime_ns
                    if not (size_grew and mtime_grew):
                        r3_fail = (
                            f"{label} R3-append no grow: size {pre_entry.size}->{post_entry.size}, "
                            f"mtime {pre_entry.mtime_ns}->{post_entry.mtime_ns}"
                        )
            else:
                r3_fail = f"{label} R3 unknown pin kind"
            if r3_fail:
                _queue_quarantine(t, r3_fail)

        # Quarantine R1-R5 failures (kill_tmux + SID clear).
        quarantined_ids: set[str] = set()
        for t in launched:
            reasons = quarantine_reasons.get(t.tab_id)
            if not reasons:
                continue
            reason = "; ".join(reasons)
            problems.append(reason)
            logger.warning("Phase R: quarantining %s — %s", t.tab_id, reason)
            try:
                await self._quarantine_codex_tab(t, reason)
            except Exception as qe:
                logger.error("Phase R quarantine for %s failed: %s", t.tab_id, qe)
                quarantine_failures.append(f"{t.tab_id}: {qe!r}")
            quarantined_ids.add(t.tab_id)

        # ---- R6/R7/R8 global/per-cwd bijection ----
        # All activity is classified relative to the ONE global snapshot taken
        # before any cold Codex launch.  A later tab's per-tab pre-scan may
        # already contain an earlier tab's new sid, so using ``any(per_tab_pre)``
        # here would hide extras and make the result order-dependent.
        for cwd_key, tabs in cwd_to_tabs.items():
            live_tabs = [t for t in tabs if t.tab_id not in quarantined_ids]
            if not live_tabs:
                continue
            expected_new = {
                t.agent_session_id
                for t in live_tabs
                if t._is_new_pin is True and t.agent_session_id
            }
            expected_append = {
                t.agent_session_id
                for t in live_tabs
                if t._is_new_pin is False and t.agent_session_id
            }
            expected_all = expected_new | expected_append
            actual_new: set[str] = set()
            actual_append: set[str] = set()
            for sid, entry in post.items():
                if not _same_cwd(entry, cwd_key):
                    continue
                pre_entry = pre_global_scan.get(sid)
                if pre_entry is None:
                    actual_new.add(sid)
                elif entry.size > pre_entry.size and entry.mtime_ns > pre_entry.mtime_ns:
                    actual_append.add(sid)

            # R6 every expected_new is in actual_new
            missing_new = expected_new - actual_new
            missing_append = expected_append - actual_append
            extras = (actual_new | actual_append) - expected_all
            batch_failures: List[str] = []
            if missing_new:
                batch_failures.append(
                    f"R6 expected new sids missing from global diff: {sorted(missing_new)}"
                )
            if missing_append:
                batch_failures.append(
                    f"R7 expected append sids missing growth from global pre-scan: {sorted(missing_append)}"
                )
            if extras:
                batch_failures.append(f"R8 extra changed sids: {sorted(extras)}")

            if batch_failures:
                batch_reason = f"cwd {cwd_key} bijection failure: " + "; ".join(batch_failures)
                problems.append(batch_reason)
                # Ownership for the cwd batch is no longer bijective.  Kill and
                # clear every still-live claimant rather than preserving a tab
                # based on launch order or a merely plausible timestamp.
                for t in live_tabs:
                    logger.warning("Phase R batch quarantine for %s — %s", t.tab_id, batch_reason)
                    try:
                        await self._quarantine_codex_tab(t, batch_reason)
                    except Exception as qe:
                        logger.error("Phase R batch quarantine for %s failed: %s", t.tab_id, qe)
                        quarantine_failures.append(f"{t.tab_id}: {qe!r}")
                    quarantined_ids.add(t.tab_id)

        if problems:
            logger.warning("Phase R reconciliation problems: %s", "; ".join(problems))
            self._save_state()
        else:
            logger.info("Phase R reconciliation OK for %d codex tabs", len(launched))
        if quarantine_failures:
            self._save_state()
            raise RuntimeError(
                "Codex reconciliation could not prove process teardown: "
                + "; ".join(quarantine_failures)
            )

    async def cleanup(self) -> None:
        """Stop ttyd processes but keep tmux sessions alive for next startup."""
        logger.info("=" * 60)
        logger.info("CLEANING UP - tmux sessions WILL BE PRESERVED")
        logger.info("=" * 60)

        async def stop_one(process: TTYDProcess) -> None:
            logger.info(
                f"Will preserve tmux session: {process.tmux_session} for tab: {process.name}"
            )
            await process.stop(kill_tmux=False)

        # Each ttyd has its own bounded wait; serial shutdown multiplies that
        # wait by the number of tabs and can exhaust the launcher's budget.
        results = await asyncio.gather(
            *(stop_one(process) for process in list(self.processes.values())),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        logger.info("Cleanup complete - all tmux sessions preserved")
