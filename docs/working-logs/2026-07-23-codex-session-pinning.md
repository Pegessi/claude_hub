# 2026-07-23: Codex (and Claude Code) session pinning across restarts

## Symptom

After a backend restart (or a machine reboot that restarts the backend),
opening a workspace and bringing back its codex/Claude Code tabs frequently
resulted in one of three failure modes:

1. **Wrong conversation**: tab A resumes tab B's chat history (and vice versa)
   — cross-wiring.
2. **Fresh session**: the CLI comes up blank, with no prior history, as if
   this were a brand new tab.
3. **Resume error**: codex/Claude complains it can't find the session and
   drops the user at a fresh prompt.

Claude tabs were partially protected (they already generated a
`--session-id <uuid>` at launch, so post-first-launch restarts were reliable),
but pre-feature tabs (existing before the `--session-id` pinning shipped) and
codex tabs had no such pinning — codex *always* used `codex resume --last`.

## Root cause

The root cause is a **cwd-race in `codex resume --last`**.

Codex CLI resume modes:

| Form | Behavior |
| --- | --- |
| `codex resume <uuid>` | resume exact session; fails if uuid not in index |
| `codex resume --last` | resume MRU session **in the current cwd** |
| `codex` (fresh) | start a new session |

Workspace agents all share the same working directory (the workspace path),
so `--last` is cwd-global. When multiple codex tabs restart in parallel,
every tab's `codex resume --last` resolves to whichever codex rollout was
most recently written in that cwd — typically the same one. N tabs → all N
point at one conversation; the other N-1 conversations are orphaned.

Additionally, codex has **no `--session-id` launch flag** (unlike
`claude --session-id <uuid>`). You cannot hand a uuid to a brand-new codex
process and ask it to bind to that id; codex assigns the uuid itself at
session start. So any pinning scheme must either:

- Discover the codex-assigned uuid after launch, or
- Redirect `CODEX_HOME` per-tab (rejected: shared memories/settings across
  tabs is a feature, not a bug).

## Design overview

We mirror the existing Claude "placeholder + discovery" pattern, with a
verification gate specific to codex.

### Lifecycle of a codex tab's `agent_session_id`

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. Construction (__init__ / state restore)                         │
│    │                                                                │
│    ├─ new tab: generate placeholder uuid4                           │
│    └─ restored tab: use stored agent_session_id from state          │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 2. Launch                                                           │
│    │                                                                │
│    ├─ fresh launch: "codex" (new session)                           │
│    └─ recover launch:                                               │
│         ├─ id is in session_index.jsonl?                            │
│         │    YES → "codex resume <uuid>"                            │
│         │    NO  → "codex resume --last"  (pre-fix behavior)        │
│         └─ both: "|| codex" fallback if resume errors               │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 3. Post-launch discovery                                            │
│    │                                                                │
│    │  _discover_codex_session_id(launch_epoch)                      │
│    │  scans ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl            │
│    │  reads first-line session_meta {session_id, cwd, timestamp}    │
│    │  matches rollouts whose timestamp is within ±window of launch  │
│    │                                                                │
│    ├─ exactly one unambiguous match → overwrite placeholder with    │
│    │   real codex uuid; persist to state                            │
│    └─ zero / ambiguous → keep placeholder; next restart will use    │
│        --last fallback (no worse than pre-fix)                      │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 4. Subsequent restarts                                              │
│    id is now a verified codex uuid in its session_index →           │
│    "codex resume <uuid>" pins the tab to ITS conversation exactly   │
└─────────────────────────────────────────────────────────────────────┘
```

### Three discovery paths

| Path | Trigger | What it does |
| --- | --- | --- |
| Startup backfill | `start_all_tabs` | `_backfill_codex_session_ids` uses tmux `session_created` timestamp as the launch epoch; matches rollouts in the tab's cwd. Thresholds: 90s match window, 600s isolation from other rollouts, 5s mtime slack (same as Claude backfill). |
| Post-bulk-start sweep | after `start_all_tabs` gathers all `start()` calls | `_discover_codex_sessions_after_start` runs discovery for any codex tab whose `agent_session_id` is not yet verified. |
| Per-tab scheduled | `create_tab`, `ensure_tab_tmux_session` returning *new* session | `_schedule_codex_discovery(process, delay=_CODEX_DISCOVERY_DELAY_S=5.0)` fires a 5s delayed task (with a 30s overall window) to let codex finish writing the rollout before scanning. |

### Key helpers added in `ttyd_manager.py`

- `_codex_home_dir()` — respects `CODEX_HOME` env var; defaults to `~/.codex`.
- `_codex_sessions_dir()` — `<codex_home>/sessions`.
- `_codex_session_start_epoch(path)` — reads first line of a rollout JSONL,
  parses the `session_meta` object, returns `(session_id, cwd, epoch_utc)` or
  `None` if unparseable.
- `_codex_candidates_for_cwd(cwd)` — globs `YYYY/MM/DD/rollout-*.jsonl` under
  the sessions dir, filters to rollouts whose `cwd` matches, returns
  `[(start_epoch, sid, path)]` sorted by epoch.
- `_codex_id_in_index(sid)` — line-scans `session_index.jsonl` for the uuid.
  Codex appends one JSON object per line; membership check is O(n) but the
  file is small (<1 MB in practice) and checked only at launch.
- `TTYDProcess._discover_codex_session_id(launch_epoch)` — the actual
  disambiguation logic; uses the same conservative isolation rule as
  `_pick_backfill_session` (if another rollout started within 600s of the
  candidate, refuse to pin).

### Verification gate (why we don't just use the placeholder, and why NOT session_index.jsonl)

A placeholder uuid4 is NOT a valid codex session. Emitting
`codex resume <placeholder-uuid>` unconditionally would:

1. Fail (uuid not in index), fall through `|| codex`, start a fresh session
   every restart → permanently lose history (WORSE than pre-fix).
2. Risk colliding with a future real codex session if we didn't overwrite.

So `_codex_launch_command(recover=True)` gates the exact-uuid path on a
verifier that checks whether the id is a codex-known session. Unverified ids
fall through to `resume --last || codex`, which is exactly the pre-fix
behavior. The gate is one-way safe: we only ever use a uuid that codex
itself told us about.

**Initial revision (review-1) gated on `~/.codex/session_index.jsonl`
membership.** Reviewer correctly blocked this: empirically 0/73 workspace-cwd
codex sessions and 0/255 CLI-launched sessions (`source: "cli"`) appear in
that file — codex only appends interactive/VSCode sessions
(`source: "vscode"`) to the index, so every Claude-Hub-spawned codex session
would have been treated as unverified and pinned resume would never fire.

The corrected gate (`_codex_id_exists`) walks codex's actual rollout files
instead, in both active (`sessions/YYYY/MM/DD/`) and archived
(`archived_sessions/`, flat) directories, reading each rollout's first-line
`session_meta.payload.session_id` (stable across resumes; falls back to
`payload.id` = filename for pre-0.143 rollouts that lack the stable-session
field). Empirically this finds 513 unique session_ids on the author's
machine (vs 240 in session_index.jsonl), correctly identifies the 73
workspace-cwd sessions, and still rejects random placeholder uuids (no
false positives). `_codex_iter_rollouts` is the shared iterator used by
both the verifier and the candidate scanners (`_codex_candidates_for_cwd`
and `_discover_codex_session_id`), so there is one source of truth for
"which rollouts count" and archived sessions are uniformly considered
everywhere (codex moves rollouts to `archived_sessions/` after ~days, but
`codex resume <uuid>` still works for them).

### Defensive fixes picked up along the way

- **Claude defensive uuid**: `_claude_session_arg()` now generates a fresh
  uuid4 if a claude tab somehow has `agent_session_id=None` at launch,
  instead of emitting the literal string `--session-id None`.
- **Agent-type change reset**: `update_tab()` regenerates
  `agent_session_id` when switching between claude/codex/cursor/terminal,
  so a uuid pinned for one CLI isn't reused against another (which would
  fail the index check and fall through, but also just be wrong).
- **State round-trip**: `agent_session_id` is serialized in
  `TTYDProcess.to_dict()` and restored from state via the
  `TTYDProcess(agent_session_id=...)` constructor kwarg, so the discovered
  codex uuid persists across restarts exactly as Claude's always has.

## Out of scope

- **Cursor `agent --continue`** uses the same cwd-race pattern (no
  `--session-id` flag, MRU-based resume). Cursor resume semantics are
  different (no on-disk session_index, different rollout format) and need
  their own investigation. Not fixed here.
- **CODEX_HOME per-tab isolation** was rejected: shared memories, config,
  and auth state across tabs is desirable; per-tab CODEX_HOME would
  fragment context and lose the benefit of codex's cross-session memory.
- **Old pre-feature "orphaned" codex sessions** that were cross-wired
  before this fix cannot be retroactively mapped to tabs — the user will
  need to manually `codex resume <uuid>` or start fresh for those.

## Validation

- **Tests** (`backend/tests/test_ttyd_manager.py`): 85 passed, 11 new:
  - `test_codex_tabs_get_placeholder_session_id` — codex gets uuid-shaped
    placeholder at construction
  - `test_cursor_and_terminal_do_not_pin_session_id` — non-CLIs stay `None`
  - `test_codex_recovery_with_pinned_id_resumes_exact_uuid` — solo mode,
    monkeypatches `_codex_id_exists` to return True, asserts
    `codex resume <uuid>` with correct solo flags
  - `test_non_solo_codex_recovery_with_pinned_id_resumes_uuid` — same for
    non-solo
  - `test_codex_session_start_epoch_parses_meta_line` — UTC-aware parse,
    prefers `payload.session_id` over `payload.id`
  - `test_codex_session_start_epoch_returns_none_for_bad_file`
  - `test_codex_id_exists_finds_session_by_rollout` — verifies rollout-based
    check (active + archived + cwd filter + placeholder rejection); replaced
    the review-1 `test_codex_id_in_index` that asserted against
    `session_index.jsonl` membership (which is empty for CLI sessions)
  - `test_codex_iter_rollouts_walks_active_and_archived` — shared iterator
    walks both directories via the `**/rollout-*.jsonl` glob
  - `test_claude_defensive_session_id_when_missing`
  - `test_codex_agent_type_change_resets_session_id`
  - `test_codex_session_id_round_trips_through_state`
  - `test_switch_env_codex_with_pinned_id_resumes_uuid` (AC6 respawn path)
- **Lint/type**: `black`, `isort --check`,
  `mypy claude_hub/services/ttyd_manager.py` all clean.
- **Empirical sanity** against the author's real `~/.codex/` (513 unique
  session_ids across active+archived rollouts; 130 workspace-cwd rollouts):
  - `_codex_id_exists(newest_workspace_sid, cwd=workspace)` returns True
    (review-1's index-based `_codex_id_in_index` returned False for 73/73
    workspace-cwd session_ids, which is why pinning never activated).
  - `_codex_id_exists(random_uuid4())` returns False (placeholder uuids
    still rejected).
- **Pre-existing test failures**: the broader workspace-manager test suite
  has a known "asyncio.run() from a running event loop" issue when the
  suite is run via background asyncio runner; foreground runs of individual
  failing tests pass. Not caused by this change.

## Risks / failure modes

- **Codex changes its on-disk layout**: the `session_meta` first-line
  format is not, to my knowledge, a public API. If codex renames the file
  or moves/removes `session_id`, the iterator returns no rollouts,
  `_codex_id_exists` returns False, and we fall back to `resume --last`
  (pre-fix behavior) — safe, just loses pinning. The parser is defensive
  (try/except around json.loads, checks for dict + session_id/id +
  timestamp fields) and returns None rather than raising.
- **`session_index.jsonl` considered harmful as a gate**: codex only
  appends interactive/VSCode-launched sessions (`source: "vscode"`) to
  this index; CLI-launched sessions (`source: "cli"`, which is what
  Claude Hub spawns) are never written there. Review-1 used this index as
  the verification gate and failed review because 0/73 workspace-cwd
  sessions were indexed. The corrected gate walks rollout files
  directly. Backward-compat alias `_codex_id_in_index = _codex_id_exists`
  keeps any lingering caller working.
- **Rollout written slowly**: if codex takes longer than 5s (the per-tab
  delay) + 30s (the discovery window) to flush the first line of the
  rollout, discovery misses. For a fresh tab, the placeholder stays in
  place until the next restart, which falls back to `--last`. Users
  restarting the backend within ~30s of creating a new codex tab may see
  pre-fix cross-wiring once, after which the id is verified.
- **Clock skew / timezone**: codex timestamps are UTC; the backfill uses
  `time.time()` (epoch UTC) directly. The comparison is epoch-vs-epoch,
  so no timezone issue is possible. The mtime slack (5s) accounts for
  filesystem timestamp granularity.
- **Concurrent discovery**: `start_all_tabs` runs all backfills and the
  post-start sweep synchronously during startup, then schedules any
  per-tab discoveries from `create_tab`/`ensure_tab_tmux_session` as
  delayed tasks. The 600s isolation rule means two tabs starting at
  nearly the same moment will both refuse to pin rather than both snap to
  the same rollout — conservative, no cross-wiring.
- **Rollout-count I/O at launch**: `_codex_id_exists` walks ~520 rollout
  files per tab launch (scaling with total codex history). Paid only at
  tab construction / recovery (not per turn); ~20 ms on the author's
  machine. If this becomes a scaling issue we can cache or build a
  short-lived in-memory index, but it's not a problem at current
  magnitudes.

## Files changed

- `backend/claude_hub/services/ttyd_manager.py` — core implementation
  (helpers, `__init__`, `_codex_launch_command`, discovery methods,
  backfill integration, defensive fixes, stale-comment updates).
- `backend/tests/test_ttyd_manager.py` — new tests, updated assertions.
- `CHANGELOG.md` — Unreleased entry.
- `docs/working-logs/2026-07-23-codex-session-pinning.md` — this file.
