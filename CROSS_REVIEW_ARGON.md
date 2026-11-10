# Adversarial Cross-Review: SQLite WAL Storage (`codex/sqlite-storage`)

- **Reviewed candidate**: `028c1d0e0ada7ae1e9adcf8b9680bac5f3701fac` (base `d8316e739fbcf3e906db08833290e532b9b05c22`)
- **Reconciled `main` base**: `4a4b02e29c8560661a7622bcfce8c8e6d687212b`
- **Rebased candidate commit**: `d5b6d1a36920a5fa39fb45dd8d1719091c8ce01f`
- **Post-review fix commit on `codex/sqlite-storage`**: `4739b65f44af4149983e2128ced4b23c950c6daf`
- **Worktree**: `/home/tiger/claude_hub_worktree/sqlite-storage`
- **Primary checkout (`/home/tiger/claude_hub`)**: untouched at `4a4b02e29c8560661a7622bcfce8c8e6d687212b` (not merged, port 8173 not restarted)
- **Applied lesson**: `a-closed-file-list-can-omit-behavior-required-by-acceptance-criteria-compare-the` (inspected all 79 changed files in `d8316e7..028c1d0` plus all 7 changed files in `d8316e7..4a4b02e`)

---

## 1. Executive Summary

We performed an adversarial cross-review across the full 79-file diff of `codex/sqlite-storage` (`028c1d0`) and reconciled it onto `main` (`4a4b02e`). The core SQLite WAL architecture—single-database consolidation (`storage.sqlite3`), `WITHOUT ROWID` collection row partitioning (`document_rows`), per-stream 32 KiB `zdict` zlib compression (`stream_events`), quarantined malformed-row retention (`stream_errors`), tombstones (`_deleted_workspaces`), and external `CLAUDE_HUB_STATE_ROOT` namespace isolation—is sound.

However, our review uncovered **1 P1 defect, 6 P2 defects, 1 P3 reconciliation defect, and 2 major performance bottlenecks** (which caused the candidate's 2.81x slower workspace cold read and ~110ms unchanged-save overhead). All defects and bottlenecks have been fixed, regression-tested, and verified on `codex/sqlite-storage` (`4739b65f44af4149983e2128ced4b23c950c6daf`).

---

## 2. Findings & Applied Fixes

### [P1] `python -m claude_hub.services.storage_maintenance` eagerly initialized the default runtime DB before acquiring `BackendInstanceLock` or parsing `--root`
- **Location**: `backend/claude_hub/services/__init__.py:1-13`, `backend/claude_hub/services/storage_maintenance.py:109-120`
- **Symptom / Root Cause**: Invoking `python -m claude_hub.services.storage_maintenance --root /path/to/copy` executes `claude_hub/services/__init__.py` before `storage_maintenance.main()`. Because `services/__init__.py` unconditionally imported `ttyd_manager` and `workspace_manager`, `TTYDManager()` and `WorkspaceManager()` instantiated at package import time and immediately ran `read_document()` against the default ambient runtime (`CLAUDE_HUB_HOME` / `~/.claude_hub`), creating `storage.sqlite3` and auto-importing legacy files before `storage_maintenance.main()` could parse `--root` or acquire `BackendInstanceLock`.
- **Fix**: Guarded the eager singleton re-exports in `backend/claude_hub/services/__init__.py` when `claude_hub.services.storage_maintenance` is invoked as a CLI entrypoint (`sys.orig_argv` / `sys.argv[0]`), while preserving normal `claude_hub.services` singleton attribute semantics for all regular callers.
- **Regression Test**: `test_storage_maintenance_cli_does_not_eagerly_initialize_default_runtime` in `backend/tests/test_local_storage.py`.

### [P2-1] `latest_unfinished_turn()` crashed with `RuntimeError` on streams containing any historical `stream_errors` row
- **Location**: `backend/claude_hub/services/agent_stream/store.py:216-247` (`inspect_turn_lifecycle` / `latest_unfinished_turn`), `backend/claude_hub/services/managed_chat.py:293-312`
- **Symptom / Root Cause**: `AgentStreamStore.latest_unfinished_turn()` delegated to `self.inspect_turn_lifecycle("__latest__")`. In the SQLite implementation, `inspect_turn_lifecycle()` queried `SELECT 1 FROM stream_errors WHERE stream = ? LIMIT 1` and raised `RuntimeError(f"Corrupt agent stream {self._path}")` whenever any historical malformed/duplicate line had been recorded in `stream_errors` during legacy JSONL import. Unlike `managed_chat._durable_turn_state()` (which catches `(OSError, RuntimeError)`), `managed_chat.cancel()` calls `await store.latest_unfinished_turn()` without catching `RuntimeError`, causing Stop/cancel and orphan turn recovery to fail with HTTP 500 on any session whose legacy JSONL ever had a single malformed line.
- **Fix**: Split `_scan_turn_lifecycle(self, turn_id: str, strict: bool)` so `inspect_turn_lifecycle(turn_id)` remains fail-closed (`strict=True`, raising `RuntimeError` when `stream_errors` exist), while `latest_unfinished_turn()` uses `strict=False` to inspect valid event rows without crashing on quarantined `stream_errors`.
- **Regression Test**: `test_latest_unfinished_turn_tolerates_historical_stream_errors_while_inspect_fails_closed` in `backend/tests/test_agent_stream_store_lifecycle.py`.

### [P2-2] `AgentStreamStore.clear()`, `replace_all()`, and `restore()` failed or wasted I/O when legacy `.jsonl` was unreadable or unimported
- **Location**: `backend/claude_hub/services/agent_stream/store.py:130-166, 377-468`
- **Symptom / Root Cause**: `_replace_rows()` and `clear()` called `self._import(db)` before deleting or replacing stream rows. If an unimported legacy `.jsonl` had `0000` permissions (or was multi-hundred-MB), `clear()`, `replace_all()`, and `restore()` raised `PermissionError` or parsed and compressed the entire legacy file into SQLite only to immediately `DELETE` all rows.
- **Fix**: Added `_ensure_stream(self, db, *, seed_events)` to register/update the `streams` metadata row directly (computing `mtime_ns`/`size` via `stat()` without reading file contents) and used it in `_replace_rows()` and `clear()`.
- **Regression Test**: `test_clear_and_replace_all_succeed_when_legacy_jsonl_is_unreadable` in `backend/tests/test_agent_stream_store_lifecycle.py`.

### [P2-3] `storage_location(path)` raised `ValueError` or created stray `storage.sqlite3` inside leaf directories when `path` was outside `runtime_home` and `state_root`
- **Location**: `backend/claude_hub/services/local_storage.py:143-172`
- **Symptom / Root Cause**:
  1. When `path` was under an external `state_root` accessed through a symlinked parent directory, `canonical_path` resolved the symlink while `path.relative_to(root)` used un-resolved paths and raised `ValueError`.
  2. When a testmonkeypatched `STATE_ROOT` on `workspace_manager` without setting `CLAUDE_HUB_STATE_ROOT`, `storage_location(path)` fell back to `return path.parent / DATABASE_FILENAME, path.name`, scattering separate `storage.sqlite3` files into `.../workspaces/<ws_id>/storage.sqlite3` and `.../agent_streams/storage.sqlite3`.
- **Fix**:
  1. Changed `path.relative_to(root)` to `canonical_path.relative_to(canonical_root)`.
  2. Added workspace layout recognition in `storage_location()` (`.../workspaces/<ws_id>/state.json` and `.../workspaces/<ws_id>/agent_streams/<s_id>.jsonl`) so custom state roots consolidate into one DB per root and namespace keys consistently.
  3. Also fixed an unported `stream.path.write_text("")` in `backend/tests/test_agent_stream_agent_image.py:1486` (`test_b4_truncated_grant_revokes_retained_bytes_and_grant_uses_no_store`) that had been masked by the old fallback creating `stream.path.parent`.
- **Regression Test**: `test_storage_location_handles_symlinked_external_state_root` in `backend/tests/test_local_storage.py`.

### [P2-4] `managed_chat.has_residual_state()` checked filesystem `cursor_path.exists()` instead of `document_exists()`
- **Location**: `backend/claude_hub/services/managed_chat.py:165-177`
- **Symptom / Root Cause**: `ManagedChatRuntimeBridge.has_residual_state()` checked `store.has_residual_state() or store.cursor_path.exists()`. Because `TailCursorStore.save()` now writes cursor state exclusively into SQLite (`documents`) and `AgentStreamStore.backup_for_edit()` writes `.jsonl.edit-bak` into SQLite (`documents`), checking `store.cursor_path.exists()` on the filesystem missed SQLite-stored cursor documents and `.jsonl.edit-bak` documents when the event stream itself was empty.
- **Fix**: Updated `has_residual_state()` in `backend/claude_hub/services/managed_chat.py` to check `document_exists(store.cursor_path)` (alongside `store.cursor_path.exists()`) and `document_exists(edit_bak)`.
- **Regression Test**: `test_has_residual_state_detects_sqlite_cursor_and_edit_backup_documents` in `backend/tests/test_managed_chat_runtime.py`.

### [P2-5] `storage_maintenance.legacy_sources()` missed orphan `.bak` files when the primary legacy JSON file was absent
- **Location**: `backend/claude_hub/services/storage_maintenance.py:21-51`
- **Symptom / Root Cause**: `legacy_sources()` only checked `path.with_name(f"{path.name}.bak")` for primary files discovered by `workspaces.glob("*/state.json")` and `workspaces.glob("*/agent_streams/*.cursor.json")`. If a workspace had only `state.json.bak` or `*.cursor.json.bak` (which `read_document()` imports via fallback recovery), `archive_legacy_files()` skipped archiving and removing the `.bak` file.
- **Fix**: Added explicit `workspaces.glob("*/state.json.bak")` and `workspaces.glob("*/agent_streams/*.cursor.json.bak")` discovery to `legacy_sources()`.
- **Regression Test**: `test_offline_archive_includes_orphan_backup_sources` in `backend/tests/test_local_storage.py`.

### [P2-6] `auth/session.py` froze `SESSIONS_FILE` at import time, and `scripts/benchmark_sqlite_storage.py` imported `claude_hub` before setting `CLAUDE_HUB_HOME`
- **Location**: `backend/claude_hub/auth/session.py:21-38`, `scripts/benchmark_sqlite_storage.py:18-28, 304-312`
- **Symptom / Root Cause**:
  1. `auth/session.py` bound `SESSIONS_FILE = resolve_runtime_home() / "sessions.json"` once at module import time. If `CLAUDE_HUB_HOME` was changed after importing `claude_hub.auth.session`, `_load_sessions()` and `_save_sessions()` still targeted the initial import-time runtime home unless `SESSIONS_FILE` was manually monkeypatched.
  2. `scripts/benchmark_sqlite_storage.py` imported `claude_hub.services.local_storage` and `claude_hub.services.workspace_manager` at module top-level before `main()` set `os.environ["CLAUDE_HUB_HOME"] = str(destination)`. In a linked worktree without `CLAUDE_HUB_HOME` pre-set, this triggered `WorkspaceManager()` against the worktree's default isolated runtime home at import time.
- **Fix**:
  1. Added `_sessions_file()` in `backend/claude_hub/auth/session.py` that dynamically resolves `resolve_runtime_home() / "sessions.json"` whenever `SESSIONS_FILE` has not been explicitly monkeypatched.
  2. Deferred `claude_hub` imports in `scripts/benchmark_sqlite_storage.py` until after `CLAUDE_HUB_HOME` and `CLAUDE_HUB_STATE_ROOT` are set to `destination`.
- **Regression Test**: `test_auth_session_default_path_never_touches_live_home_in_linked_worktree` in `backend/tests/test_auth_session_runtime_isolation.py`.

### [P3 / Reconciliation with `main` @ `4a4b02e`] `runtime.py` unused import & capability metadata preservation + multi-instance/multiprocess stream append reconciliation
- **Location**: `backend/claude_hub/api/routes/agent_stream/runtime.py:22, 255-287`, `backend/tests/test_agent_stream_store_lifecycle.py`
- **Symptom / Root Cause**:
  1. `main` (`4a4b02e`) modified `runtime.py`, `observation.py`, `services/agent_stream/store.py`, and `tests/test_agent_stream_store_lifecycle.py`.
  2. In `runtime.py`, `SessionKind` was unused (flagged by Ruff `F401`), and `unavailable()` discarded transport capability fields (`structured_live`, `input_live`, `history_source`) when `not caps.structured`.
  3. `4a4b02e` added `test_store_instances_coordinate_appends_for_same_session` (8 instances x 80 appends) and `test_store_appends_across_processes` (3 processes x 20 appends) using raw file reads (`store.path.read_text()`), which conflicted with SQLite authority.
- **Fix**: Rebased `codex/sqlite-storage` cleanly onto `4a4b02e`, removed unused `SessionKind` from `runtime.py`, preserved `base.model_copy(...)` in `unavailable(reason, caps)`, and ported `4a4b02e`'s multi-instance and multiprocess append tests to verify SQLite storage (`store.read_since(-1, 1000)`) under shared `CLAUDE_HUB_HOME` / `CLAUDE_HUB_STATE_ROOT`.

---

## 3. Performance Investigation & Low-Risk Optimizations

The candidate report noted two performance regressions/costs:
1. **Full workspace read**: 2.81x slower than JSON (`165.45 ms` vs `58.95 ms`).
2. **Workspace save**: `~110 ms` unchanged-save serialization overhead (`109.85 ms` vs `185.10 ms` JSON).

### Root Causes Identified by Profiling
1. **Per-call connection & PRAGMA churn**: Every `read_document()` and `write_document()` call opened and closed a fresh `sqlite3.Connection` and re-executed `os.chmod`, `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=FULL`, `PRAGMA foreign_keys=ON`, `PRAGMA temp_store=MEMORY`, and `PRAGMA user_version` under `_SCHEMA_LOCK`.
2. **Repeated zlib decompression + JSON parsing of 6,337 unchanged rows on every read**: `state.json` is partitioned into 1 layout row + 6,337 `document_rows` (tasks, reports, sessions, etc.). Every `read_document(state.json)` decompressed and `json.loads()`-parsed all 6,337 rows (~150 ms) even when 99.9% of rows had identical SHA-256 digests.
3. **Stdlib `json.dumps(sort_keys=True)` on 6,337 items on every save**: `write_document(state.json)` serialized all 6,337 items in pure Python (`~75 ms`) before comparing row SHA-256 digests.

### Low-Risk Optimizations Applied in `backend/claude_hub/services/local_storage.py`
1. **Per-thread SQLite connection cache (`_THREAD_LOCAL.connections`) + per-`(dev, ino)` one-time schema initialization (`_INITIALIZED_DATABASES`)**: Eliminates repeated `sqlite3.connect()`, `chmod`, and `PRAGMA` execution on every operation while automatically reopening if the database file is replaced, unlinked, or moved. Added `close_cached_connections()` for explicit cleanup.
2. **Content-addressed row & document caches (`_ROW_JSON_CACHE` & `_DOC_JSON_CACHE`)**:
   - `_ROW_JSON_CACHE` caches decoded immutable JSON bytes by row SHA-256 `digest` (`SELECT key, digest FROM document_rows` fetches only compact metadata for cached rows and skips fetching/decompressing unchanged `body` blobs).
   - `_DOC_JSON_CACHE` caches a deep-copied assembled document keyed by `(layout_sha256, tuple((key, row_digest), ...))`. Because SQLite is queried first for current `documents.body` and `(key, digest)` pairs on every read, cross-process writes, rollbacks, or database replacements immediately invalidate the digest key—preserving 100% consistency and corruption detection (`DatabaseError` on corrupt `body` when `read_document` validates uncached rows).
3. **Rust-backed `pydantic_core.to_json` fast path (`_json_bytes`)**: Uses `pydantic_core.to_json` for fast canonical UTF-8 JSON serialization of `dict`/`list` payloads when available, falling back to `json.dumps` for custom types.

### Measured Benchmark Comparison (7-Workspace Production Copy, 6,337 Partitioned Rows)

| Metric | Legacy JSON | Candidate `028c1d0` | Optimized `4739b65` | Improvement vs `028c1d0` |
| --- | ---: | ---: | ---: | ---: |
| Full workspace read (`_load_state` median) | 51.94 ms | 165.45 ms | **58.44 ms** | **2.83x faster** (matches JSON) |
| Cached `read_document` median | 51.94 ms | 150.25 ms | **12.57 ms** | **11.95x faster** |
| Full workspace save (`_save_state`, 1 changed task) | 185.10 ms | 109.85 ms | **46.35 ms** | **2.37x faster** (4.0x faster than JSON) |
| Last 100 stream events (`read_tail(100)`) | 571.10 ms | 2.98 ms | **2.98 ms** | **191x faster than JSONL** |

---

## 4. Verification Summary

All verification commands were executed in `/home/tiger/claude_hub_worktree/sqlite-storage` with isolated runtime directories:

1. **`./scripts/verify.sh all`**: **PASS**
   - `docs`: PASS (`AGENTS.md` and `CLAUDE.md` byte-identical; single `## Released` heading in `CHANGELOG.md`)
   - `checkout-safety`: PASS
   - `backend-format`: PASS (`641 files would be left unchanged`)
   - `backend-types`: PASS (`Success: no issues found in 623 source files`)
   - `backend-tests`: PASS (`6260 passed, 10 skipped, 16 warnings in 444.21s`, 86% total coverage)
   - `frontend-lint`: PASS
   - `frontend-types`: PASS
   - `frontend-tests`: PASS (`1279 passed, 0 failed`)
   - `frontend-build`: PASS (`built in 9.75s`)
2. **`./scripts/verify.sh check-staged`**: **PASS**
3. **Supplemental Ruff check on all touched production files**: **PASS** (`All checks passed!`)
4. **Primary checkout & live service invariants**:
   - `/home/tiger/claude_hub` `HEAD` remains at `4a4b02e29c8560661a7622bcfce8c8e6d687212b` (`main`).
   - Live Hub service on port `8173` was NOT restarted and `/home/tiger/.claude_hub` was NOT mutated.
