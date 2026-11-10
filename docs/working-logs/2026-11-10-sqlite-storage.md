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

Reference run on the fixed copy (decimal MB, after Argon cross-review optimizations):

| Measurement | JSON/JSONL | SQLite + recovery | Change |
| --- | ---: | ---: | --- |
| Active dataset | 1,997.23 MB | 1,076.40 MB | 46.1% smaller |
| Total, retaining compressed migration sources | 1,997.23 MB | 1,387.22 MB | 30.5% smaller |
| Full workspace read | 51.9 ms | 58.4 ms | 1.12x (near parity; down from 165.5 ms) |
| Workspace persistence (changed) | 185.1 ms | 46.3 ms | 4.00x faster (down from 109.8 ms) |
| Workspace persistence (unchanged) | 185.1 ms | 45.4 ms | 4.07x faster (down from 109.7 ms) |
| Last 100 stream events | 586.0 ms | 3.11 ms | 188x faster |

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

A final root-switching probe found that distinct external `CLAUDE_HUB_STATE_ROOT`
values initially shared their `workspaces/...` keys. External roots now use
separate namespaces derived from canonical directory identity within the same
runtime database. A→B→A reads, stream identity, directory aliases and namespace
retirement are tested together; retiring A cannot erase B's records. Offline
archival also honors the configured external workspace root.


## Workspace review round 2: provider snapshot ownership

The reviewer reproduced an edit-resend cleanup bug: suffix-based dispatch treated
a provider `*.jsonl.edit-bak` sidecar as a Hub SQLite document, creating a database
next to provider transcripts. Cleanup now dispatches by explicit owner: the Hub
store deletes its SQLite snapshot, while provider snapshot cleanup only unlinks
its file. No filename heuristic remains. The real manager edit-resend regression
covers both successful send and rollback after send failure, asserts the Hub
snapshot is gone, and enumerates provider files to reject stray SQLite artifacts.
The retired-session path already clears its Hub snapshot through store.clear; its
remaining sidecar cleanup stays file-only. Final validation is attached to Task.


## Argon adversarial cross-review and main (4a4b02e) reconciliation

Task: `3adfb932-f439-4632-9876-94212e10410d`. Rebased `codex/sqlite-storage` cleanly onto
`main` @ `4a4b02e29c8560661a7622bcfce8c8e6d687212b`, retaining `4a4b02e`'s provider-failure
history fallback (`observation.py`, `runtime.py`, and regression tests) alongside SQLite WAL
persistence. Fixed all findings from the adversarial cross-review (see `CROSS_REVIEW_ARGON.md`):

- **Lazy service package exports (`services/__init__.py`) and CLI runtime isolation (`storage_maintenance.py`, `benchmark_sqlite_storage.py`)**: replaced eager package-level singleton imports with PEP 562 lazy `__getattr__` exports backed by `_ServicesPackage(types.ModuleType)` (preventing same-named submodule loads from shadowing `ttyd_manager` / `workspace_manager` singletons) so importing `claude_hub.services.*` submodules never eagerly instantiates managers against the default runtime home before `--root` / `--destination` and `BackendInstanceLock` are acquired.
- **Tolerant Stop vs strict retry lifecycle inspection (`agent_stream/store.py`)**: restored `main`'s separation between `latest_unfinished_turn()` (tolerant of historical `stream_errors` rows so UI Stop and orphan recovery remain functional on migrated streams with legacy malformed lines) and `inspect_turn_lifecycle()` (strictly fail-closed on `stream_errors` or `OSError`).
- **Source-free stream clear/replace/restore (`agent_stream/store.py`)**: `_clear_rows`, `_replace_rows`, and `restore` now register or look up the `streams` marker via `_ensure_stream` without importing legacy `.jsonl` contents that are immediately being deleted/replaced, seeding `streams.dictionary` from uncompressed `_json_bytes` of `seed_events[:16]` and allowing `clear()` / `replace_all()` to succeed even when an unimported legacy file is unreadable.
- **Tombstone & corrupt-blob handling (`workspace_manager/_persistence.py`, `local_storage.py`)**: `_load_durable_json()` now catches `FileNotFoundError` from tombstoned SQLite documents (`delete_document` / `retire_namespace`) and returns `_DurableLoadResult(data=None, source="empty")` without resurrecting stale on-disk files; `unpack()`, `read_document()`, and `connection()` translate `zlib.error` into `OSError`.
- **SQLite cursor & snapshot residual-state detection (`managed_chat.py`)**: `has_residual_state()` now checks `document_exists(store.cursor_path)` and `document_exists(<stream>.edit-bak)` so SQLite-only cursors and snapshots reserve session identity until retired.
- **Primary/CI checkout compatibility (`auth/session.py`, `test_auth_session_runtime_isolation.py`) and custom non-`workspaces` state-root resolution (`local_storage.py`)**: dynamic `_sessions_file()` resolution, deterministic linked-worktree guard testing, and unified `storage_location` parent resolution for `workspaces`/`agent_streams`/`state.json`/`.namespace`.
- **Orphan backup archival (`storage_maintenance.py`)**: `legacy_sources()` now explicitly globs `*/state.json.bak` and `*/agent_streams/*.cursor.json.bak` so backups retained after missing-primary recovery are archived.
- **Read/write performance optimization (`local_storage.py`)**: replaced per-item `json.dumps` with byte-identical `pydantic_core.to_json` (`_json_bytes`), moved `_parts_and_digests` outside `BEGIN IMMEDIATE` via `_PREPARED_WRITE`, and added bounded content-addressed SHA-256 LRU caches (`_RAW_BY_DIGEST` and `_DOC_JSON_CACHE`) with single-pass `cached_raw_by_key` lookup (avoiding cache-eviction TOCTOU), reducing full workspace read latency from 165.5 ms to 58.4 ms (2.83x faster) and workspace save latency from 109.8 ms to 46.3 ms (2.37x faster).

