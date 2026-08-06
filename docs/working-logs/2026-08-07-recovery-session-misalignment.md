# Fix: Prevent Codex/Cursor Session Cross-Wiring on Cold Restart

**Date:** 2026-08-07
**Branch:** `fix/recovery-session-misalignment`
**Task:** `b7ff5c97-12f0-4917-9c5b-7a4921907217` ("agent/terminal恢复功能修复")
**GP:** v16 (approved 4/4)

## Symptom

After a full service cold restart (ttyd/tmux server gone, not a backend-only
hot restart), same-type agent sessions (codex↔codex, cursor↔cursor,
claude↔claude) could resume each other's conversations. In production this
manifested as worker `cb-agent-1` or reviewer `cb-reviewer-4` opening inside
another tab's rollout history after restart.

## Root Cause Analysis (BUG-1 through BUG-5)

Five interacting bugs in `services/ttyd_manager.py`:

| # | Bug | Why it caused cross-wiring |
|---|-----|----------------------------|
| BUG-1 | Single global `launch_epoch` shared across all tabs | Every concurrently-launching codex inherited the same epoch; ts windows overlapped completely. |
| BUG-2 | `_CODEX_DISCOVERY_DELAY_S = 5` + `_CODEX_DISCOVERY_WINDOW_S = 600` | Ten-minute-wide window happily matched any other codex rollout written "recently", including old sessions from the same cwd. |
| BUG-3 | `codex resume --last \|\| codex` and `codex resume --continue <id> \|\| codex resume --last \|\| codex` fallback chain | When the persisted sid did not exist on disk (e.g. first launch after the bug), `--last` grabbed whichever rollout was newest in the cwd — usually a *different tab's* rollout. |
| BUG-4 | Per-tab `_discover_codex_session_id(launch_epoch)` did N independent scans during concurrent launches | No global lock → two tabs launching at the same instant saw the same set of new rollouts and each picked one, sometimes swapping. |
| BUG-5 | `launch_epoch` stamped *after* `await process.start()`, which does `await asyncio.sleep(1)` while ttyd binds | Codex already ran and wrote its session_meta during that 1 s sleep, so the epoch was ~1 s late and new-pin windows started *after* the rollout mtime. |

Additional V0 empirical findings that fed the fix:

- Codex 0.146.1 writes rollouts as JSONL where the first line is always
  `{type: session_meta, payload: {id, session_id, cwd, timestamp}}`. The
  canonical SID is `payload.id` (matches the filename uuid);
  `payload.session_id` is the fork-thread id and differs on forked sessions.
- `~/.codex/sessions/YYYY/MM/DD/` is date-partitioned for active rolls;
  `~/.codex/archived_sessions/` is FLAT (not date-partitioned) — the old
  `_codex_rollout_dirs()` only walked active partitions, missing archived.
- Cursor CLI (`agent`) is a **constructive pin**: `agent --resume <uuid>`
  creates `store.db` immediately even for arbitrary uuids, never errors. So
  we do NOT need a quarantine path for cursor unknown-sids; we just always
  pin and always resume.

## Fix Design (v16)

Five fixes F1-F6 plus reconciliation R1-R8:

### F1 — Remove `--last`/`--continue` fallback (BUG-3)
`_codex_launch_command(recover=True)` now only issues `codex resume <sid>`
when the sid is verified on disk (`_codex_id_exists(sid, cwd)` returns True,
with cwd-realpath matching). Otherwise starts `codex` fresh. No more
cwd-global fallback.

### F2 — Global launch lock + per-cwd batches with atomic rollback (BUG-1, BUG-4)
New `GLOBAL_CODEX_LAUNCH_LOCK = asyncio.Lock()` serializes all cold codex
launches. Tabs are grouped by `os.path.realpath(cwd)`. Within each cwd:
1. Verified-non-quarantined tabs launch FIRST (so their appends are observed
   before any fresh codex in the same cwd starts writing).
2. Fresh/unverified/quarantined tabs launch second.
3. If any tab in the cwd throws, `stop(kill_tmux=True)` every started tab in
   that cwd, set `resume_quarantined=True` for the whole group, clear all
   launch state, and save. Cross-cwd groups are independent.

### F3 — Fence-poll signal replaces 600s window (BUG-2)
After launching each cold codex, poll every 200ms for up to 30 attempts (~6s
wall), observing `_diff_scans(pre, post)` filtered to same-cwd
`new`/`appended` entries. Wait an additional `_CODEX_FENCE_SILENCE_S = 0.7s`
after the last observed change before accepting the signal. This gives tight
attribution without the 600s blast radius.

### F4 — Per-tab clock anchor in `ensure_tmux_session` (BUG-5)
TTYDProcess gains `_launch_wall` (wall clock) and `_launch_mono` (monotonic),
stamped immediately **before** `tmux new-session -d` is spawned, not after
start(). New-pin ts window is `[-2s, +8s]` around `_launch_wall`.

### F5 — Phase R reconciliation (R1-R8 bijection)
After all codex tabs in a cold batch have launched, re-scan and enforce:

| Rule | Check | Fail action |
|------|-------|-------------|
| R1 | Every pinned sid exists in post-scan | quarantine tab |
| R2 | Every entry.cwd realpath matches tab.cwd realpath | quarantine tab |
| R3-new | New-pin sid.ts falls in [-2s, +8s] window vs _launch_wall | quarantine tab |
| R3-append | Append-resume sid grew size AND mtime_ns vs _pre_scan | quarantine tab |
| R4 | agent_session_id is non-empty | quarantine tab |
| R5 | No two tabs pinned to the same sid | quarantine latter tab |
| R6 | Every expected new-pin sid is in actual-new per cwd | log problems (tab already R1-R5 safe) |
| R7 | Every expected append-resume sid was present in pre_scan | log problems |
| R8 | Any new same-cwd sids NOT mapped to an expected tab → quarantine every unpinned tab in that cwd | whole-cwd unpinned quarantine |

Crucially, R8 ensures a tab whose own rollout is indistinguishable from a
stray rollout (concurrent external launch, tmux glitch, etc.) is quarantined
rather than mis-pinned. On the next restart that quarantined tab starts
fresh and gets a correctly-attributed new sid.

### F6 — Cursor constructive pin + `_cursor_verify.py`
- Cursor always pins a uuid4 at `TTYDProcess.__init__` (previously only
  claude/codex got generated sids); `--resume <uuid>` is always passed.
- `services/_cursor_verify.py` implements two-tier verification:
  - **Tier 1 (authoritative):** open `<home>/.cursor/chats/<md5(realpath(cwd))>/<chatId>/store.db`
    read-only via URI, read `meta.key = 0` (hex-encoded JSON), compare
    `agentId` field to the sid.
  - **Tier 2 (legacy fallback):** walk sibling `meta.json` files whose
    realpath-cwd matches and check that the chat-directory name equals the
    sid.
- No quarantine path for cursor: `agent --resume <uuid>` always succeeds
  (constructive pin verified in V0), so we do not need fail-closed here;
  `|| agent` fallback handles any resume error by starting fresh.

### Persisted state: `resume_quarantined`
New boolean on each tab, default False (back-compat), serialized into
`tabs.json`. Set True by atomic rollback or Phase-R failure; cleared on
successful `FRESH_PIN` or `RESUME_OK`. Quarantined tabs never issue `codex
resume`; they start fresh next launch.

## Files Changed

- `backend/claude_hub/services/ttyd_manager.py` — main fix: new helpers
  (`_parse_session_meta`, `_codex_roots`, `_codex_scan_sessions`,
  `_diff_scans`, `_codex_id_exists` with cwd parameter,
  `_codex_candidates_for_cwd`, `_codex_poll_signal`,
  `_launch_one_cold_codex_locked`, `_reconcile_codex_phase_r`,
  `_schedule_codex_discovery`); new constants; TTYDProcess gets
  `resume_quarantined`, `_launch_wall`, `_launch_mono`, `_is_new_pin`,
  `_pre_scan`; cursor always gets a uuid4; `start_all_tabs()` rewritten as
  Phase 0/1/1S/4/F lifecycle; removed old helpers `_codex_sessions_dir`,
  `_codex_session_start_epoch`, `_codex_rollout_dirs`, `_codex_iter_rollouts`,
  `_discover_codex_session_id`.
- `backend/claude_hub/services/_cursor_verify.py` — NEW. Two-tier cursor
  session verification (store.db meta key=0 authoritative, meta.json
  realpath fallback).
- `backend/tests/test_ttyd_manager.py` — updated for new helper signatures
  and cursor-pinning behavior; new tests `test_parse_session_meta_*`,
  `test_codex_scan_sessions_walks_active_and_archived`,
  `test_diff_scans_classifies_new_appended_attr`,
  `test_codex_id_exists_finds_session_by_rollout`,
  `test_resume_quarantined_round_trips_through_state`,
  `test_codex_quarantined_does_not_issue_resume`,
  `test_cursor_pins_session_id_terminal_does_not`;
  removed `test_cursor_and_terminal_do_not_pin_session_id` (outdated
  expectation); updated launch-command tests to expect no `--last`/`--continue`.
- `backend/tests/test_codex_sessions.py` — updated to monkeypatch
  `_codex_scan_sessions` (returning `Dict[str, ScanEntry]`) instead of the
  removed `_codex_iter_rollouts` (generator); rollout fixtures now include
  `payload.id` (canonical SID per V0 finding).
- `backend/tests/test_recovery_integration_v4v6.py` — NEW. 8 integration
  tests exercising V4/V6 scenarios with subprocess stubbed (tmux/ttyd calls
  intercepted; codex rollouts created on-disk with realistic format and
  timing).
- `CHANGELOG.md` — Unreleased/Fixes entry.

## Validation Summary

| Check | Result |
|-------|--------|
| V0 empirical probes (codex rollout format, archived roots, fork id vs session_id, cursor constructive-pin behavior) | Verified by hand before coding |
| V1 `_parse_session_meta` new-payload / legacy / bad-line | Unit tests pass |
| V2 `_codex_scan_sessions` walks active + archived flat, prefers active>archived, highest mtime | Unit + integration pass |
| V3 `_diff_scans` new/appended/attr_changed classification | Unit test passes |
| V4-A archived_sessions flat layout is discovered | Integration test passes |
| V4-B verified-resume appends to target rollout (RESUME_OK) | Integration test passes |
| V4-C same-cwd 3 codex (2 verified + 1 fresh) attribute to 3 distinct sids, none quarantined | Integration test passes |
| V4-D cursor always pins uuid4 and issues `agent --resume <sid>` | Integration test passes; `test_cursor_recovery_uses_resume_uuid` unit test passes |
| V4-E quarantined tab does NOT resume; FRESH_PIN clears quarantine | Integration test passes |
| V4-F R8 extra same-cwd stray rollout → quarantine unpinned tab | Integration test passes |
| V5 `resume_quarantined` round-trips through to_dict/from_dict/state persist | Unit test passes |
| V6 single-tab cold launch pins new sid and is discoverable via `_codex_id_exists` | Integration test passes |
| V7 `test_codex_sessions.py` endpoint tests (grouping, sort, dedup, title-boilerplate skip) | All pass |
| V8 detached-baseline pytest on main@2144b4a vs fix branch | Baseline 549 passed / 61 pre-existing event-loop failures; fix 553 passed / same 61 failures. **Zero new failures.** (+4 is the new unit tests added in this PR) |
| V9 `black` / `isort` / `mypy` on all touched files | All clean |
| V10 existing launch-command expectations (no `--last`, cursor `--resume`, quarantined codex skips resume, solo-mode flags) | Updated and pass |

## Risks / Trade-offs

1. **Serial codex launches**: under GLOBAL_CODEX_LAUNCH_LOCK, N cold codex
   tabs take ~N × (0.5–1.5 s) wall time instead of launching in parallel. In
   practice cold starts are rare (only full service / tmux-server restarts)
   and N is small (< 10). Hot reattaches (backend-only restarts, the common
   case) still launch in parallel in Phase 1.
2. **Quarantine is conservative**: any Phase-R anomaly quarantines rather
   than risks cross-wiring. Quarantined codex tabs start fresh on the next
   launch and lose the previous session's history, which matches the
   existing UX for codex tabs that fail to resume. User impact: an anomalous
   cold restart may require the user to look up the prior session in the
   Codex session list instead of having it auto-resumed. Better than
   resuming someone else's session.
3. **Cursor DB format coupling**: Tier 1 reads `meta.key = 0` as hex-JSON
   with `agentId`. If Cursor ships a new format that changes this,
   verification falls back to Tier 2 (meta.json chatId directory name
   match), then to failing open ("unknown" → start fresh with a new uuid4,
   which is always correct for the constructive-pin model — it just creates
   a new chat rather than resuming the old one).
4. **Timestamp-based attribution**: R3-new uses a [-2s, +8s] window around
   `_launch_wall`. V0 measurements showed codex writes session_meta ~0.1–2s
   after process start on a warm cache; the +8s upper bound is generous for
   cold-cache / slow-fs scenarios. A rollout written >8s after launch is
   treated as not-ours, which can quarantine a very slow start but will not
   misattribute.

## Follow-ups / Out of scope

- **Claude cross-wiring?** Claude CLI already supports `--session-id <uuid>`
  at launch, which deterministically pins and does not rely on post-hoc
  rollout discovery. The claude path was already correct; this PR does not
  change it (verified by examining `_claude_command`).
- **Cursor session resume for very old Cursor versions** that do not write
  `agentId` in store.db meta: handled by Tier 2 fallback (meta.json chatId
  directory name).
- **Remote execution targets (ExecutionTarget.REMOTE):** the lock and
  per-cwd batching apply only to LOCAL targets; remote codex launches were
  not reported to exhibit cross-wiring and retain their existing path.
