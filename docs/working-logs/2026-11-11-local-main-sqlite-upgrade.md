# Local main synchronization and SQLite cutover preparation

## Request and ownership

Task `950e2fa7-a587-4c4a-84bb-be0136025946` is initiator-controlled.
The user requested the latest main and migration of this Mac's local persistence.
The clean primary checkout was `945d06546def0123090651b734e90dca3aecfa75`.
Fetched upstream is `87802e6ae0eb2e9ba37ea4d5b04aa5b71ae42cdd`.
Local main had ten unique commits; upstream had 135. Neither history was reset,
rebased or dropped. No remote push or unrelated worktree cleanup is authorized.

Integration happened in `~/claude_hub_worktree/main-sqlite-upgrade-20261111` on
`codex/main-sqlite-upgrade-20261111`. Candidate merge
`6fe7f2f209e23ca19ac6a8d389e7f7a705f8eadb` retains both parents. The sole native
conflict was the watchdog import list: retain upstream human activity labels and
local substantive-answer validation. Independent review confirmed that local
post-tool/final-result/EOF handling remains and the new labels have their state
and call sites. The adjacent decision record binds that immutable merge tree.
No additional application behavior was introduced by this synchronization.

## Backup and rehearsal evidence

The primary backend is PID 57455, started November 9, listening on 8173 without
reload. It remains the legacy JSON/JSONL writer. The live home had no
`storage.sqlite3`, WAL or SHM before preparation, and remains without one pending
cutover authorization. The primary Hub was not stopped or restarted.

An owner-only backup of the migration tool's bounded source inventory lives at
`~/.claude_hub_backups/sqlite-upgrade-20261111T073804Z/`. Its 112 input files total
388,643,954 bytes. A SHA-256 manifest covers every copied input; all hashes were
rechecked unchanged. Credentials and conversation content stay in private backup
and ignored evidence files. Other configuration, provider transcripts, attachment
blobs and unrelated runtime files retain their existing owners and were not moved.

An isolated, separate rehearsal copy ran the supported `storage_maintenance
--pre-import` path under the actual production Python 3.12 interpreter:

- 17 documents and 87 streams imported; all 614,818 valid events were compared
  individually with the source, including identities, payloads and ordering.
- No malformed or unfinished trailing rows were present in this snapshot.
  Verification explicitly checks the raw malformed-row evidence and deferred tails.
- SQLite schema version 1, `PRAGMA integrity_check=ok`.
- A second `--sync-legacy` wrote zero stream rows.
- Owner-equivalent structural checks passed: six indexed workspaces, 485 tasks,
  seven sessions, 8,934 reports, 8,265 Task events, four schedules/128 runs,
  36 tabs with no dangling order entries, 390 inbox messages/three questions,
  one Goal, 11 subagents, two Bots, 269 login sessions and 170 Feishu dedup records.

The source inventory also contains a seventh workspace state outside the
six-entry index; its bytes, records and stream namespace were retained and checked.
No missing index entry was guessed or silently discarded.

## Validation and limits

Locked feature dependencies were installed with Python 3.11.16 and uv 0.12.15.
The production virtual environment was not modified. Node 24.13.0 differs from the
repository's 24.21.0 pin; the frontend project uses pnpm 9.15.9, while the outer
verification version probe sees the host's pnpm 10.29.3.

`./scripts/verify.sh all` ran. Docs, checkout safety, frontend lint, frontend types
and frontend build passed. Its three backend targets were rejected before running
because macOS Python lacks the required `waitid/WNOWAIT` ownership support. This
is an unrun standard backend check, not a pass. Supplemental direct checks used
the feature interpreter, isolated HOME/XDG/Hub/state/tmp paths and a private tmux
socket: Black and isort passed, mypy passed all 619 source files, and the focused
storage/auth/stream/lifecycle suite passed 292 tests in 16.10 seconds. These results
do not replace a complete standard backend suite on a supported host.

Frontend unit tests reported 1,449/1,450 passing. The live-gap repair-read test at
`chatFeatureLifecycle.test.mjs:521` failed its initial-read assertion in both full
runs; its focused rerun from the frontend cwd passed. The frontend Git tree is
identical to upstream `87802e6a`. This existing timing-sensitive result is disclosed,
not hidden by repeated full-suite retries or an unrelated source change.

The immutable merge decision check passed with complete coverage of its one
conflict. `verify.sh check-staged` passed before the merge commit. Browser and live
external-provider gates were not run; this work adds no frontend behavior.
Private detailed evidence is under `.agent_office/sqlite-upgrade/` in the task
worktree; that directory and virtual environments are not committed.

## Cutover is pending explicit primary-service authorization

A populated live database is deliberately not left behind while the old backend
continues writing JSON. Its completion markers would make a later new backend
prefer a stale pre-import snapshot. `--sync-legacy` can also overwrite a newer
non-tombstoned SQLite document; it is solely a pre-cutover catch-up tool and must
never be run after SQLite becomes authoritative.

The remaining controlled cutover needs an authorized short stop of the old 8173
writer, a fresh bounded backup/inventory, final schema checks and supported import,
full content/integrity verification, then start of the new backend and readback.
Recheck additions, rewrites and deletions against the snapshot; legacy sync does
not manufacture tombstones for disappeared source files. If an authoritative
SQLite database has appeared in the meantime, stop and reassess rather than
importing legacy files over it. Keep all JSON/JSONL and backups; no archival or
source deletion is required for this cutover.

The user's AGENTS.md explicitly says not to stop/restart the primary Hub on
5173/8173, so that final primary-service operation requires a specific exception.
Preparation, backup and rehearsal are complete; they do not mean the running
service has switched its authoritative persistence to SQLite.
