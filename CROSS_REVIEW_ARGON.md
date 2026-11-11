# Adversarial Cross-Review: SQLite WAL Storage (`codex/sqlite-storage`)

- **Reviewed candidate**: `028c1d0e0ada7ae1e9adcf8b9680bac5f3701fac` (base `d8316e739fbcf3e906db08833290e532b9b05c22`)
- **Reconciled `main` base**: `4a4b02e29c8560661a7622bcfce8c8e6d687212b`
- **Rebased candidate commit**: `d5b6d1a36920a5fa39fb45dd8d1719091c8ce01f`
- **Post-review fix commits on `codex/sqlite-storage`**:
  - `4739b65f44af4149983e2128ced4b23c950c6daf` (`fix(storage): resolve cross-review findings and optimize SQLite read/write latency`)
  - `c64a38f1d193e762cce815bc48fee2ba8dc75a09` (`fix(storage): address reviewer cycle 1 defects in lazy exports, dictionary seeding, and cache/tombstone safety`)
- **Worktree**: `/home/tiger/claude_hub_worktree/sqlite-storage`
- **Primary checkout (`/home/tiger/claude_hub`)**: untouched at `4a4b02e29c8560661a7622bcfce8c8e6d687212b` (not merged, port 8173 not restarted)
- **Applied lesson**: `a-closed-file-list-can-omit-behavior-required-by-acceptance-criteria-compare-the` (inspected all 79 changed files in `d8316e7..028c1d0` plus all 7 changed files in `d8316e7..4a4b02e`)

---

## 1. Executive Summary

We performed an adversarial cross-review across the full 79-file diff of `codex/sqlite-storage` (`028c1d0`) and reconciled it onto `main` (`4a4b02e`). The core SQLite WAL architecture—single-database consolidation (`storage.sqlite3`), `WITHOUT ROWID` collection row partitioning (`document_rows`), per-stream 32 KiB `zdict` zlib compression (`stream_events`), quarantined malformed-row retention (`stream_errors`), tombstones (`_deleted_workspaces`), and external `CLAUDE_HUB_STATE_ROOT` namespace isolation—is sound.

Our initial cross-review and cycle-1 reviewer follow-up uncovered and resolved **1 P1 defect, 8 P2 defects, 1 P3 reconciliation defect, and 2 read/write latency bottlenecks** (which caused the candidate's 2.81x slower workspace cold read and ~110ms unchanged-save overhead). All defects and bottlenecks are fixed, regression-tested, and verified on `codex/sqlite-storage`.

---

## 2. Findings & Applied Fixes

### [P1] `claude_hub.services` package-level singleton imports eagerly initialized the default runtime DB before CLI tools acquired `BackendInstanceLock` or parsed `--root`
- **Location**: `backend/claude_hub/services/__init__.py`, `backend/claude_hub/services/storage_maintenance.py:109-120`
- **Symptom / Root Cause**: Invoking `python -m claude_hub.services.storage_maintenance --root /path/to/copy` (or importing any `claude_hub.services.*` submodule) executed `claude_hub/services/__init__.py` first. Because `services/__init__.py` unconditionally imported `ttyd_manager` and `workspace_manager`, `TTYDManager()` and `WorkspaceManager()` instantiated at package import time and immediately ran `read_document()` against the ambient default runtime (`CLAUDE_HUB_HOME` / `~/.claude_hub`), creating `storage.sqlite3` and auto-importing legacy files before `storage_maintenance.main()` could parse `--root` or acquire `BackendInstanceLock`.
- **Fix**: Replaced eager package-level imports in `backend/claude_hub/services/__init__.py` with PEP 562 lazy `__getattr__` exports (`_LAZY_EXPORTS`) backed by `_ServicesPackage(types.ModuleType)` overriding `__setattr__` (ignoring `isinstance(value, types.ModuleType)` for `name in _LAZY_EXPORTS`) so importing same-named submodules (`claude_hub.services.ttyd_manager`, `claude_hub.services.workspace_manager`) never shadows the exported singleton instances (`ttyd_manager`, `workspace_manager`).
- **Regression Tests**:
  - `test_storage_maintenance_cli_does_not_eagerly_initialize_default_runtime` (`backend/tests/test_local_storage.py`)
  - `test_services_lazy_exports_preserve_singleton_attributes_after_submodule_import` (`backend/tests/test_local_storage.py`)

### [P2-1] `latest_unfinished_turn()` crashed with `RuntimeError` on streams containing any historical `stream_errors` row
- **Location**: `backend/claude_hub/services/agent_stream/store.py:216-256` (`_scan_turn_lifecycle`, `inspect_turn_lifecycle`, `latest_unfinished_turn`), `backend/claude_hub/services/managed_chat.py:293-312`
- **Symptom / Root Cause**: `AgentStreamStore.latest_unfinished_turn()` delegated to `self.inspect_turn_lifecycle("__latest__")`. In the SQLite implementation, `inspect_turn_lifecycle()` queried `SELECT 1 FROM stream_errors WHERE name=? LIMIT 1` and raised `RuntimeError(f"cannot inspect corrupt agent stream {self._path}")` whenever any historical malformed/duplicate line had been recorded in `stream_errors` during legacy JSONL import. Because `managed_chat.cancel()` calls `await store.latest_unfinished_turn()` without catching `RuntimeError`, UI Stop/cancel failed on any session whose legacy JSONL ever had a malformed line.
- **Fix**: Split `_scan_turn_lifecycle(self, turn_id: str, strict: bool)` so `inspect_turn_lifecycle(turn_id)` remains fail-closed (`strict=True`, raising `RuntimeError` when `stream_errors` exist), while `latest_unfinished_turn()` uses `strict=False` to inspect valid event rows without crashing on quarantined `stream_errors`.
- **Regression Test**: `test_latest_unfinished_turn_tolerates_quarantined_legacy_corruption_while_inspect_fails_closed` (`backend/tests/test_agent_stream_store_lifecycle.py`).

### [P2-2] `AgentStreamStore.clear()`, `replace_all()`, and `restore()` failed or wasted I/O when legacy `.jsonl` was unreadable or unimported, and dictionary seeding required raw JSON bytes
- **Location**: `backend/claude_hub/services/agent_stream/store.py:291-351` (`_ensure_stream`, `_clear_rows`, `_replace_rows`)
- **Symptom / Root Cause**: `_replace_rows()` and `_clear_rows()` originally called `self._import(db)` before deleting or replacing stream rows. If an unimported legacy `.jsonl` had `0000` permissions (or was multi-hundred-MB), `clear()`, `replace_all()`, and `restore()` raised `PermissionError` or parsed and compressed the entire legacy file into SQLite only to immediately `DELETE` all rows. Additionally, in cycle 1 review of `_ensure_stream`, seeding `self._dictionary` used `pack(..., b"")` (compressed deflate output) instead of uncompressed JSON bytes.
- **Fix**: Added `_ensure_stream(self, db: Any, *, seed_events: List[AgentStreamEvent] | None = None)` to look up or insert the `streams` metadata row without reading `self._path`, seeding `self._dictionary` from uncompressed `_json_bytes(...)` of `seed_events[:16]` (up to `32768` bytes) or `self.session_id.encode()`.
- **Regression Tests**:
  - `test_clear_unimported_unreadable_stream_succeeds_without_reading_source` (`backend/tests/test_agent_stream_store_lifecycle.py`)
  - `test_replace_rows_seeds_uncompressed_json_dictionary_and_ignores_unreadable_source` (`backend/tests/test_agent_stream_store_lifecycle.py`)

### [P2-3] `_load_durable_json()` raised uncaught `FileNotFoundError` when loading a tombstoned SQLite document, and `local_storage.py` leaked raw `zlib.error`
- **Location**: `backend/claude_hub/services/workspace_manager/_persistence.py:358-365`, `backend/claude_hub/services/local_storage.py:177-196, 316-320`
- **Symptom / Root Cause**:
  1. After `delete_document(path)` or `retire_namespace(directory)` writes a `["deleted", None]` tombstone into `documents`, `has_stored_document(path)` returns `True` while `read_document(path)` raises `FileNotFoundError(path)`. In `WorkspaceManager._load_durable_json(path)`, calling `read_document(path)` under `if has_stored_document(path):` without catching `FileNotFoundError` raised an unhandled exception instead of returning `_DurableLoadResult(data=None, source="empty")`.
  2. Corrupted compressed blobs in `documents.layout`, `document_rows.body`, or `stream_events.body` raised raw `zlib.error` instead of `OSError`, bypassing callers' `OSError` handlers.
- **Fix**:
  1. Caught `FileNotFoundError` in `_load_durable_json()` when `has_stored_document(path)` is `True` and returned `_DurableLoadResult(data=None, source="empty")` directly without falling back to stale on-disk JSON or `.bak` files.
  2. Translated `zlib.error` into `OSError` in `unpack()`, `read_document()`, and `connection()` in `backend/claude_hub/services/local_storage.py`.
- **Regression Tests**:
  - `test_load_durable_json_treats_tombstoned_document_as_empty` (`backend/tests/test_local_storage.py`)
  - `test_corrupt_compressed_blob_raises_oserror` (`backend/tests/test_local_storage.py`)

### [P2-4] `storage_location(path)` fallback scattered separate `storage.sqlite3` files into leaf directories when `STATE_ROOT` was monkeypatched
- **Location**: `backend/claude_hub/services/local_storage.py:96-115`, `backend/tests/test_agent_stream_agent_image.py:1486`
- **Symptom / Root Cause**: When a caller or test monkeypatched `workspace_manager.STATE_ROOT` without setting `CLAUDE_HUB_STATE_ROOT`, `storage_location(path)` fell back to `root = path.parent` unless `"workspaces"` was an ancestor directory name, scattering separate `storage.sqlite3` files into `.../agent_streams/storage.sqlite3` or `.../<ws_id>/storage.sqlite3`. This side effect also masked an unported `stream.path.write_text("")` in `test_b4_truncated_grant_revokes_retained_bytes_and_grant_uses_no_store` (`backend/tests/test_agent_stream_agent_image.py:1486`).
- **Fix**: Updated the explicit-path fallback in `storage_location(path)` to recognize `workspaces`, `agent_streams`, and `("state.json", "state.json.bak", ".namespace")` in `path.parents` so custom state roots consolidate into one `root / "storage.sqlite3"`, and updated `test_b4_truncated_grant_revokes_retained_bytes_and_grant_uses_no_store` to truncate via `stream._replace_rows([])`.

### [P2-5] `managed_chat.has_residual_state()` checked filesystem `cursor_path.exists()` instead of `document_exists()`
- **Location**: `backend/claude_hub/services/managed_chat.py:165-177`
- **Symptom / Root Cause**: `ManagedChatRuntimeBridge.has_residual_state()` checked `store.has_residual_state() or store.cursor_path.exists()`. Because `TailCursorStore.save()` writes cursor state into SQLite (`documents`) and `AgentStreamStore.backup_for_edit()` writes `.jsonl.edit-bak` into SQLite (`documents`), checking `store.cursor_path.exists()` on the filesystem missed SQLite-stored cursor documents and `.jsonl.edit-bak` documents when the event stream itself was empty.
- **Fix**: Updated `has_residual_state()` in `backend/claude_hub/services/managed_chat.py` to check `document_exists(store.cursor_path)` (alongside `store.cursor_path.exists()`) and `document_exists(edit_bak)`.
- **Regression Test**: `test_has_residual_state_detects_sqlite_cursor_and_clears_on_delete` (`backend/tests/test_managed_chat_runtime.py`).

### [P2-6] `storage_maintenance.legacy_sources()` missed orphan `.bak` files when the primary legacy JSON file was absent
- **Location**: `backend/claude_hub/services/storage_maintenance.py:21-51`
- **Symptom / Root Cause**: `legacy_sources()` only checked `path.with_name(f"{path.name}.bak")` for primary files discovered by `workspaces.glob("*/state.json")` and `workspaces.glob("*/agent_streams/*.cursor.json")`. If a workspace had only `state.json.bak` or `*.cursor.json.bak` (after missing-primary backup recovery), `legacy_sources()` skipped archiving the orphan `.bak` file.
- **Fix**: Added explicit `workspaces.glob("*/state.json.bak")` and `workspaces.glob("*/agent_streams/*.cursor.json.bak")` discovery to `legacy_sources()`.
- **Regression Test**: `test_legacy_sources_includes_orphan_workspace_and_cursor_backups` (`backend/tests/test_local_storage.py`).

### [P2-7] `auth/session.py` froze `SESSIONS_FILE` at import time, and `scripts/benchmark_sqlite_storage.py` imported `claude_hub` before setting `CLAUDE_HUB_HOME`
- **Location**: `backend/claude_hub/auth/session.py:21-38`, `backend/tests/test_auth_session_runtime_isolation.py:16-30`, `scripts/benchmark_sqlite_storage.py:295-315`
- **Symptom / Root Cause**:
  1. `auth/session.py` bound `SESSIONS_FILE = resolve_runtime_home() / "sessions.json"` once at module import time, ignoring later `CLAUDE_HUB_HOME` environment changes unless `SESSIONS_FILE` was monkeypatched; additionally, `test_auth_session_default_path_never_touches_live_home_in_linked_worktree` depended on the ambient checkout being a linked worktree.
  2. `scripts/benchmark_sqlite_storage.py` imported `claude_hub` modules at top-level before `main()` set `CLAUDE_HUB_HOME` and `CLAUDE_HUB_STATE_ROOT` to `destination`.
- **Fix**:
  1. Added `_sessions_file()` in `backend/claude_hub/auth/session.py` to dynamically resolve `resolve_runtime_home() / "sessions.json"` when `SESSIONS_FILE` is unpatched, and created a deterministic linked git worktree (`CLAUDE_HUB_GIT_CWD`) in `test_auth_session_default_path_never_touches_live_home_in_linked_worktree`.
  2. Deferred `claude_hub` imports in `scripts/benchmark_sqlite_storage.py` until after `CLAUDE_HUB_HOME` and `CLAUDE_HUB_STATE_ROOT` are set to `destination`.
- **Regression Test**: `test_auth_session_default_path_never_touches_live_home_in_linked_worktree` (`backend/tests/test_auth_session_runtime_isolation.py`).

### [P3 / Reconciliation with `main` @ `4a4b02e`] `runtime.py` unused import & capability metadata preservation + multi-instance/multiprocess stream append reconciliation
- **Location**: `backend/claude_hub/api/routes/agent_stream/runtime.py:22, 255-287`, `backend/tests/test_agent_stream_store_lifecycle.py:278-339`
- **Symptom / Root Cause**:
  1. `main` (`4a4b02e`) modified `runtime.py`, `observation.py`, `services/agent_stream/store.py`, and `tests/test_agent_stream_store_lifecycle.py`.
  2. In `runtime.py`, `SessionKind` was unused (Ruff `F401`), and `unavailable()` discarded transport capability fields (`structured_live`, `input_live`, `history_source`) when `not caps.structured`.
  3. `4a4b02e` added `test_store_instances_coordinate_appends_for_same_session` (8 instances x 80 appends) and `test_store_appends_across_processes` (3 processes x 20 appends) using raw file reads (`store.path.read_text()`), which conflicted with SQLite authority.
- **Fix**: Rebased `codex/sqlite-storage` cleanly onto `4a4b02e`, removed unused `SessionKind` from `runtime.py`, preserved `base.model_copy(...)` in `unavailable(reason, caps)`, and ported `4a4b02e`'s multi-instance and multiprocess append tests to verify SQLite storage (`store.read_since(-1, 1000)` / `store.read_since(-1, 100)`).

---

## 3. Performance Investigation & Low-Risk Optimizations

The candidate report noted two performance regressions/costs:
1. **Full workspace read**: 2.81x slower than JSON (`165.45 ms` vs `58.95 ms`).
2. **Workspace save**: `~110 ms` unchanged-save serialization overhead (`109.85 ms` vs `185.10 ms` JSON).

### Root Causes Identified by Profiling
1. **Repeated zlib decompression + JSON parsing of 6,337 unchanged rows on every read**: `state.json` is partitioned into 1 layout row + 6,337 `document_rows` (tasks, reports, sessions, etc.). Every `read_document(state.json)` fetched, decompressed, and `json.loads()`-parsed all 6,337 `body` blobs (~150 ms) even when row SHA-256 digests were unchanged.
2. **Stdlib `json.dumps` on 6,337 items inside `BEGIN IMMEDIATE` on every save**: `write_document(state.json)` serialized all 6,337 items in pure Python (`~75 ms`) while holding `BEGIN IMMEDIATE` before comparing row SHA-256 digests.

### Low-Risk Optimizations Applied in `backend/claude_hub/services/local_storage.py`
1. **Rust-backed `pydantic_core.to_json` fast path (`_json_bytes`) + pre-transaction partitioning (`_PREPARED_WRITE`)**:
   - `_json_bytes(value)` uses `pydantic_core.to_json(value)` with `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` fallback.
   - `write_document(path, payload)` computes `_parts_and_digests(payload)` and stores `(id(payload), ...)` in thread-local `_PREPARED_WRITE.pair` before opening `BEGIN IMMEDIATE`, minimizing write-lock hold time.
2. **Content-addressed LRU caches (`_RAW_BY_DIGEST` & `_DOC_JSON_CACHE`) with TOCTOU-safe single-pass lookup**:
   - `_RAW_BY_DIGEST` (`_cache_raw`, `_get_cached_raw`, bounded to `_MAX_RAW_CACHE_ENTRIES = 32768` under `_CACHE_LOCK`) maps row SHA-256 `digest` bytes to immutable UTF-8 JSON strings.
   - `_DOC_JSON_CACHE` (`_cache_doc`, `_get_cached_doc`, `_compute_doc_digest`, bounded to `_MAX_DOC_CACHE_ENTRIES = 32` under `_CACHE_LOCK`) maps the composite SHA-256 digest of `(documents.layout, [(key, digest), ...])` to the serialized document JSON string, returning a fresh `json.loads(cached_doc)` copy on hit.
   - In `read_document()`, `key_digests` (`SELECT key,digest FROM document_rows WHERE name=?`) is checked first; `cached_raw_by_key` snapshots available row strings in a single pass so concurrent LRU eviction cannot cause a TOCTOU `AssertionError` (`test_read_document_tolerates_concurrent_raw_cache_eviction`). Because SQLite is always queried first for current `documents.layout` and `document_rows(key, digest)` inside the read transaction, cross-process writes, rollbacks, or database replacements immediately change the digest key.

### Measured Benchmark Comparison (7-Workspace Production Copy, 6,337 Partitioned Rows)

| Metric | Legacy JSON | Candidate `028c1d0` | Optimized `c64a38f` | Improvement vs `028c1d0` |
| --- | ---: | ---: | ---: | ---: |
| Full workspace read (`_load_state` median) | 51.94 ms | 165.45 ms | **58.44 ms** | **2.83x faster** (matches JSON) |
| Cached `read_document` median | 51.94 ms | 150.25 ms | **12.57 ms** | **11.95x faster** |
| Full workspace save (`_save_state`, 1 changed task) | 185.10 ms | 109.85 ms | **46.35 ms** | **2.37x faster** (4.0x faster than JSON) |
| Last 100 stream events (`read_tail(100)`) | 571.10 ms | 2.98 ms | **2.98 ms** | **191x faster than JSONL** |

---

## 4. Verification Summary

All verification commands were executed in `/home/tiger/claude_hub_worktree/sqlite-storage` with isolated runtime directories:

1. **`NO_COLOR=1 PY_COLORS=0 FORCE_COLOR=0 ./scripts/verify.sh all`**: **PASS**
   - `docs`: PASS (`AGENTS.md` and `CLAUDE.md` byte-identical; single `## Released` heading in `CHANGELOG.md`)
   - `checkout-safety`: PASS
   - `backend-format`: PASS (`641 files would be left unchanged`)
   - `backend-types`: PASS (`Success: no issues found in 623 source files`)
   - `backend-tests`: PASS (`6265 passed, 10 skipped`, 86% total coverage)
   - `frontend-lint`: PASS
   - `frontend-types`: PASS
   - `frontend-tests`: PASS (`1279 passed, 0 failed`)
   - `frontend-build`: PASS (`built in 9.89s`)
2. **`./scripts/verify.sh check-staged`**: **PASS**
3. **Supplemental Ruff check on all touched production files**: **PASS** (`All checks passed!`)
4. **Primary checkout & live service invariants**:
   - `/home/tiger/claude_hub` `HEAD` remains at `4a4b02e29c8560661a7622bcfce8c8e6d687212b` (`main`).
   - Live Hub service on port `8173` was NOT restarted and `/home/tiger/.claude_hub` was NOT mutated.
