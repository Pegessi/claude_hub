# Unified local SQLite storage

Task: `78aa60c9-7dd7-4b0a-b566-4b7ed6ec6d66`. Base:
`d8316e76044a443ec52aef7fa6ca94e7d1e4b567`. Candidate worktree:
`/home/tiger/claude_hub_worktree/sqlite-storage`, branch `codex/sqlite-storage`.
No production cutover, service restart, merge or push is part of this delivery.
An isolation incident during an early helper test is recorded below.

## Audit and design

The initial read-only audit found 353 JSONL streams occupying 1,880,790,259 bytes,
six workspace primaries occupying 22,737,481 bytes and equal rotating backups.
Top-level files: chat messages 2,204,020; Goals 456,782; tabs 433,028;
login sessions 363,982; subagents 107,215; Feishu deduplication 54,556 bytes.
The live source continued growing; benchmark numbers below use a fixed copy.
Each workspace mutation previously serialized the whole board, copied/fsynced the
old primary, wrote/fsynced a replacement and regenerated Markdown. Its file-write
amplification was at least twice the workspace size, excluding Markdown.
Each token delta added all event metadata and each cold read reparsed preceding
JSONL rows. The audit found 22 malformed stream rows; none are silently discarded.

The wider bounded JSON inventory also covered workspace index/schedules, stream
cursors, Feishu pool secrets, tab order, Chat inbox, feedback lessons/drafts/runs,
attachment indexes, environment profiles, sidebar preferences, restart controls,
managed delivery-attempt evidence, and Dot's repository/journal/index.
A later footprint inventory (live, excluding task-owned runtime worktrees) found
261 non-stream workspace JSON files at 35.09 MB, Dot JSON at 469 KB, launch-env
files at 110 KB, sidebar preferences at 231 bytes, env presets at 2.4 KB,
restart controls at 3 KB and private configuration at 2.3 KB. These low-volume
configuration/artifact/evidence files retain their separate domain lifecycles;
provider-owned transcripts are external inputs. They are not competing mirrors
of the migrated authoritative stores. Dot's derived SQLite index is unchanged.
The benchmark dataset covers all migrated sources and their old backups, not the
entire runtime home, provider histories, logs or attachment blobs.

Prior commits `6947a63` and `817628f` were inspected. Their transactions, import
markers, model fidelity and rollback invariants were reused in the design, while
their JSON/shadow/dual/SQLite modes, sync mirror and controller were not imported.
There is one engine, `services/local_storage.py`, and no permanent dual writes.

- `CLAUDE_HUB_HOME/storage.sqlite3` uses WAL, `synchronous=FULL`, a 30-second busy
  timeout, owner-only permissions and schema version 1. Explicit workspace-root
  overrides map logical workspace names into the same runtime database.
- Documents partition collection entries into stable rows. Hashes skip unchanged
  bodies; one transaction changes rows and their ordering/layout together.
  Domain validation, ownership locks and report/mailbox deduplication remain in
  their existing owners. This does not add a distributed read-modify-write API.
- Streams retain every valid event and raw sequence, using numeric stream keys,
  rowid storage for large blobs, indexes by sequence/turn/type/tool identity, and
  lossless zlib dictionaries seeded from each source. No lossy text coalescing or
  cursor renumbering is required to achieve savings.
- First import, invalid-row evidence and the completion marker commit together.
  Invalid/duplicate/missing-sequence rows remain in `stream_errors`; lifecycle
  evidence fails closed on those rows. Original bytes remain recoverable.
- Readers use WAL snapshots after import; they do not reserve the writer lock.
  SQL history projection replaces the file-offset/in-memory index and lifecycle
  file-stat cache. Image/Markdown authorization, feedback evidence and handoff
  receipts use the same durable records. Rollback snapshots live in SQLite.
- Clear/deletion retains empty/tombstone markers so old sources cannot revive
  deleted records. Resource fencing still precedes artifact retirement.

## Migration, archival and recovery

Normal domain loads automatically import their existing logical JSON paths.
Workspace legacy validation/quarantine and backup recovery happen before import;
a recovered backup is committed before returning from the loader. Invalid SQLite
state does not fall back to a stale JSON source. Interrupted imports roll back and
retry; reruns do not overwrite committed SQLite records. Do not run the old
backend against retained JSON as though it were current state.

Sources are retained until explicit offline archival. With the backend stopped,
`backend/.venv/bin/python -m claude_hub.services.storage_maintenance --root ROOT`
acquires the same backend ownership lock, verifies the import marker, writes an
owner-only gzip archive, fsyncs and checks both archive and unchanged source before
unlinking that source. Existing mismatched archives are never overwritten.
The archives preserve original migration input, including malformed rows; they
are not a backup of subsequent SQLite transactions or a live rollback mirror.
For current-state backup use SQLite's online backup API, or stop all writers and
copy the database plus any WAL. Restore with writers stopped. Keep migration
archives for forensic/recovery needs; credential-bearing archives require the
same owner-only treatment as the database. No live archives were created here.

## Fixed-copy benchmark

Reproducible command from the worktree:

```bash
backend/.venv/bin/python scripts/benchmark_sqlite_storage.py \
  --source /home/tiger/.claude_hub \
  --destination /tmp/hub-storage-benchmark-20261110-v4 --archive
```

The tool refuses an existing initial destination or a destination outside `/tmp`.
A SHA-256 manifest pins copied inputs. `--reuse-copy --verify-replay` restores only
that isolated copy from verified archives, reruns against the same inputs, and
compares every valid source event to SQLite replay. Its final `result.json` and
Task report carry the final-candidate evidence; no conversation contents are
included in reports. Benchmarks include compression/decompression, warm median
reads, JSON backup/rewrite and a changed-record SQLite save. Derived Markdown
rendering is excluded from both persistence timing paths. Full workspace cold
loads remain slower; pagination is where indexed reads give the largest gain.

Reference run on the fixed copy (decimal MB):

| Measurement | JSON/JSONL | SQLite + recovery | Change |
| --- | ---: | ---: | --- |
| Active dataset | 1,997.23 MB | 1,076.41 MB | 46.1% smaller |
| Total, retaining compressed migration sources | 1,997.23 MB | 1,387.23 MB | 30.5% smaller |
| Full workspace read | 60.8 ms | 160.2 ms | 2.64x slower |
| Workspace persistence | 187.4 ms | 108.5 ms | 1.73x faster |
| Last 100 stream events | 571.1 ms | 2.98 ms | 191x faster |

The reference run imported 2,219,946 valid events and preserved 22 malformed rows,
with `PRAGMA integrity_check=ok`; import took 242.2 seconds and a marker-only rerun
0.25 seconds. Final measurements additionally exercise changed-record writes and
full replay comparison, so timing variation is reported in the final Task report.
The initial per-event layout saved only 2.3%; dictionary compression and avoiding
WITHOUT ROWID overflow-page waste were selected from measured results.
Automatic import alone retains the original files and initially increases total
disk usage; the total reduction above requires the explicit verified archival
step. Allow headroom for originals, SQLite and migration WAL during cutover.

## Verification and handoff

Focused tests exercise first import, malformed and corrupt input, backup recovery,
rollback after insert/replace failure, concurrent stream owners, cancellation,
read/write concurrency, unchanged-write WAL frames, custom roots, tombstones,
archival mismatch, edit-resend, history/pagination, handoff and cleanup.
Repository validation uses `./scripts/verify.sh all` and `check-staged`; it includes
complete backend mypy and tests plus Black/isort and frontend checks. Supplemental
Ruff runs check all production code's critical errors and all new storage/tool
modules with default rules; existing wildcard/facade import conventions are not
silently removed. Exact results, skips and independently reviewed candidate SHA
are delivered through the Task report. Browser/HMR gates are not required because
no frontend behavior changed. Human acceptance remains required.

Applied lesson: `a-closed-file-list-can-omit-behavior-required-by-acceptance-criteria-compare-the`.
Review covers the full base-to-candidate diff and consumers discovered through
behavioral regressions, rather than treating the initial file list as exhaustive.

## Independent review fixes and isolation incident

Independent review of `3531e12` found two defects: authentication retained a
hardcoded default-home path, and one compact lifecycle-body reader conflated a
nullable epoch with epoch zero. Both are fixed with regression tests. Auth path
selection is now tested through a fresh subprocess import using fake HOME and an
isolated runtime, without monkeypatching the sessions-file constant. Explicit
legacy paths also pass the existing linked-worktree live-home guard. Nullable
and zero lifecycle epochs remain distinct.

The auth defect had already affected an early helper's direct pytest run:
it created a schema-v0 SQLite artifact in the live home. A bounded read-only
inspection found only the sessions namespace, with all 1,016 existing legacy
sessions identical and 24 additional test records in that new artifact. The
legacy JSON's content was not modified. No process held the new SQLite files.
They were preserved with owner-only permissions outside the active runtime at
`/tmp/hub-storage-isolation-evidence-20261110`, together with a content-free
provenance record. The accidental database is absent from the live home; it must
never be promoted as production state. No shared service was restarted.
This exception is disclosed rather than claiming there were no live-home writes.
The actual migration/archival benchmarks operated only on their `/tmp` copy.

The fixed-snapshot replay check compared all 2,219,946 valid events, preserving
all 22 malformed rows separately; integrity remained `ok`. Final-candidate
reruns and the independent re-review are attached to the Task report.
